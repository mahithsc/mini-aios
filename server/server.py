from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from aios_core.initialize import register_runtime_shutdown, shutdown_runtime, start_runtime
from aios_core.sessions import get_chat_artifacts_dir
from server.execution.runtime import get_runs_service, shutdown_runs_service, start_runs_service
from server.cloud_events import shutdown_cloud_device_events, start_cloud_device_events
from server.types.run import RunCreateRequest
from server.gateway.routes import router as gateway_router
from server.lights import lights
from server.notifications.runtime import shutdown_notification_service, start_notification_service
from server.transcriptions import TranscriptionResponse, transcribe_upload
from server.updater import router as updater_router
from server.uploads import save_uploads

register_runtime_shutdown()
log = logging.getLogger(__name__)


async def _submit_codex_continuation(
    manager,
    runs_service,
    session_id: str,
    job_id: str,
    signal: str,
) -> bool:
    """Claim one durable signal and enqueue its main-agent continuation."""

    if not manager.store.claim_signal(job_id, signal):
        return False
    try:
        run = await runs_service.submit_run(
            RunCreateRequest(
                kind="chat",
                chatId=session_id,
                sourceId=f"codex:{job_id}",
                turnId=signal,
            )
        )
    except Exception:
        manager.store.release_signal(job_id, signal)
        raise
    if signal == "done":
        manager.store.update(job_id, verification_status="queued")
        manager.emit_status(
            job_id,
            "codex.verification.queued",
            {"status": "queued", "continuation_run_id": run.id},
        )
    manager.store.complete_signal(job_id, signal, continuation_run_id=run.id)
    log.info(
        "Codex continuation accepted",
        extra={
            "codex_job_id": job_id,
            "codex_signal": signal,
            "continuation_run_id": run.id,
        },
    )
    return True


def _install_codex_sinks() -> None:
    """Stream live Codex progress (codex.* events) onto the gateway bus so the chat
    shows what a background Codex job is doing. The CodexJob reader thread calls the
    sink; we hop back to this loop (bus.publish is not thread-safe) before publishing."""
    from aios_core.tools.codex_job import (
        _manager,
        set_lifecycle_sink,
        set_progress_sink,
    )
    from server.gateway.bus import get_gateway_bus

    loop = asyncio.get_running_loop()
    bus = get_gateway_bus()

    def sink(session_id: str, event_type: str, payload: dict) -> None:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            bus.publish(session_id, event_type, payload)
            return

        delivered = asyncio.run_coroutine_threadsafe(
            _publish_gateway_event(bus, session_id, event_type, payload), loop
        )
        delivered.result(timeout=5)

    set_progress_sink(sink)

    async def submit_continuation(
        session_id: str, job_id: str, signal: str
    ) -> None:
        await _submit_codex_continuation(
            _manager,
            get_runs_service(),
            session_id,
            job_id,
            signal,
        )

    def lifecycle_sink(session_id: str, job_id: str, signal: str) -> None:
        def schedule() -> None:
            task = asyncio.create_task(
                submit_continuation(session_id, job_id, signal),
                name=f"codex-continuation-{job_id}-{signal}",
            )
            task.add_done_callback(_log_background_failure)

        loop.call_soon_threadsafe(schedule)

    set_lifecycle_sink(lifecycle_sink)
    for event in _manager.store.pending_gateway_events():
        try:
            bus.publish(event["session_id"], event["event_type"], event["payload"])
            _manager.store.complete_gateway_event(event["job_id"], event["sequence"])
        except Exception:
            log.exception(
                "Failed to replay Codex gateway event %s:%s",
                event["job_id"],
                event["sequence"],
            )
    _manager.reconcile_stale()
    for job_id, signal in _manager.store.pending_signals():
        record = _manager.store.get(job_id)
        session_id = record.get("session_id") if record else None
        if isinstance(session_id, str) and session_id:
            lifecycle_sink(session_id, job_id, signal)
    removed = _manager.cleanup()
    if removed:
        log.info("Removed %s expired Codex run records", removed)


async def _publish_gateway_event(bus, session_id: str, event_type: str, payload: dict) -> None:
    bus.publish(session_id, event_type, payload)


def _log_background_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.error(
            "Codex continuation submission failed",
            exc_info=(type(error), error, error.__traceback__),
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_runtime()
    await lights.start()
    await start_notification_service()
    await start_cloud_device_events()
    await start_runs_service()
    _install_codex_sinks()
    try:
        yield
    finally:
        from aios_core.tools.codex_job import (
            _manager,
            set_lifecycle_sink,
            set_progress_sink,
        )

        set_lifecycle_sink(None)
        set_progress_sink(None)
        await asyncio.to_thread(_manager.stop_all)
        await lights.shutdown()
        await shutdown_cloud_device_events()
        await shutdown_notification_service()
        await shutdown_runs_service()
        shutdown_runtime()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Billing, accounts, and Stripe/Supabase live in the cloud service (aios-cloud),
# never on the device box. The box authenticates LAN callers with a local token
# (see server.gateway.routes.require_gateway_auth) and holds no cloud secrets.
app.include_router(gateway_router)
app.include_router(updater_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/attachments")
async def upload_attachments(
    chatId: str = Form(...),
    files: list[UploadFile] = File(...),
) -> dict[str, object]:
    attachments = await save_uploads(chatId, files)
    return {"attachments": [attachment.model_dump(mode="json") for attachment in attachments]}


@app.post("/transcriptions", response_model=TranscriptionResponse)
async def create_transcription(
    file: UploadFile = File(...),
    startedAt: int | None = Form(None),
    endedAt: int | None = Form(None),
    mimeType: str | None = Form(None),
) -> TranscriptionResponse:
    return await transcribe_upload(
        file,
        started_at=startedAt,
        ended_at=endedAt,
        mime_type=mimeType,
    )


@app.get("/session-artifacts/{chat_id}/{artifact_path:path}")
async def get_session_artifact_file(chat_id: str, artifact_path: str) -> FileResponse:
    artifacts_root = get_chat_artifacts_dir(chat_id).resolve()
    requested_relative_path = Path(artifact_path)
    requested_path = (artifacts_root / requested_relative_path).resolve()

    try:
        requested_path.relative_to(artifacts_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Artifact not found.") from exc

    if not requested_path.exists() or not requested_path.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found.")

    return FileResponse(requested_path)
