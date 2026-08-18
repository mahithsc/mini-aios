from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from aios_core.execution.runtime import shutdown_runs_service, start_runs_service
from aios_core.initialize import (
    register_runtime_shutdown,
    shutdown_runtime,
    start_runtime,
)
from aios_core.sessions import get_chat_artifacts_dir
from server.cloud_events import (
    cloud_device_events_enabled,
    shutdown_cloud_device_events,
    start_cloud_device_events,
)
from server.gateway.routes import router as gateway_router
from server.gateway.run_broadcaster import RunBroadcaster
from server.lights import lights
from server.notifications.runtime import (
    shutdown_notification_service,
    start_notification_service,
)
from server.transcriptions import TranscriptionResponse, transcribe_upload
from server.updater import router as updater_router
from server.uploads import save_uploads

register_runtime_shutdown()


def _install_pi_progress_sink() -> None:
    """Stream live Pi progress (pi.* events) onto the gateway bus so the chat
    shows what a background Pi job is doing. The PiJob reader thread calls the
    sink; we hop back to this loop (bus.publish is not thread-safe) before publishing."""
    from aios_core.agent.pi.runtime import set_progress_sink
    from server.gateway.bus import get_gateway_bus

    loop = asyncio.get_running_loop()
    bus = get_gateway_bus()

    def sink(session_id: str, event_type: str, payload: dict) -> None:
        loop.call_soon_threadsafe(bus.publish, session_id, event_type, payload)

    set_progress_sink(sink)


@asynccontextmanager
async def lifespan(_: FastAPI):
    start_runtime()
    await lights.start()
    await start_notification_service()
    receive_cloud_events = cloud_device_events_enabled()
    await start_runs_service(
        broadcaster=RunBroadcaster(),
        activity=lights,
    )
    _install_pi_progress_sink()
    try:
        if receive_cloud_events:
            await start_cloud_device_events()
        yield
    finally:
        from aios_core.agent.pi.runtime import set_progress_sink

        set_progress_sink(None)
        if receive_cloud_events:
            await shutdown_cloud_device_events()
        await lights.shutdown()
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
    return {
        "attachments": [
            attachment.model_dump(mode="json") for attachment in attachments
        ]
    }


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
