from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse

from aios_core.sessions import (
    get_chat_metadata,
    list_chat_history,
    load_chat_session,
    save_chat_session,
    update_chat_status,
    update_chat_title,
)
from aios_core.tools.codex_job import _manager as codex_job_manager
from server.execution.runtime import get_runs_service
from server.notifications.runtime import get_notification_service
from server.types.chat import AssistantMessage, ChatMetadata, UserMessage
from server.types.notification import (
    Notification,
    NotificationDismissRequest,
    NotificationListResponse,
)
from server.types.run import RunCreateRequest
from server.updater import require_accepting_work

from .bus import get_gateway_bus
from .schemas import (
    CodexAnswers,
    EventOut,
    MessageCreate,
    MessageOut,
    MessageSubmitOut,
    SessionCreate,
    SessionOut,
    ms_to_iso_z,
)
from .store import list_gateway_events_after

_SSE_KEEPALIVE_SECONDS = 15.0

_MANIFEST_TO_GATEWAY_STATUS = {
    "idle": "idle",
    "streaming": "running",
    "error": "failed",
    "cancelled": "idle",
}


def require_gateway_auth(authorization: str | None = Header(default=None)) -> None:
    token = os.getenv("AIOS_GATEWAY_TOKEN")
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token.")


router = APIRouter(dependencies=[Depends(require_gateway_auth)])


@router.get("/notifications", response_model=NotificationListResponse)
async def list_notifications() -> NotificationListResponse:
    return get_notification_service().list_notifications()


@router.post("/notifications/dismiss", response_model=Notification)
async def dismiss_notification(body: NotificationDismissRequest) -> Notification:
    notification = get_notification_service().dismiss_notification(body.id)
    if notification is None:
        raise HTTPException(status_code=404, detail="notification not found")
    return notification


@router.get("/notifications/events")
async def stream_notification_events() -> StreamingResponse:
    service = get_notification_service()

    async def event_source() -> AsyncIterator[str]:
        queue = service.broadcaster.subscribe()
        try:
            while True:
                try:
                    message = await asyncio.wait_for(
                        queue.get(), timeout=_SSE_KEEPALIVE_SECONDS
                    )
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield (
                    f"event: {message['type']}\n"
                    f"data: {json.dumps(message)}\n\n"
                )
        finally:
            service.broadcaster.unsubscribe(queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _get_chat_or_404(session_id: str) -> ChatMetadata:
    chat = get_chat_metadata(session_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="session not found")
    return chat


def _active_chat_ids() -> set[str]:
    snapshots = get_runs_service().list_active_runs(kinds=["chat"])
    return {snapshot.chatId for snapshot in snapshots if snapshot.chatId}


def _gateway_status(chat: ChatMetadata, active_chat_ids: set[str] | None = None) -> str:
    status = _MANIFEST_TO_GATEWAY_STATUS.get(chat.status or "idle", "idle")
    if status == "running":
        # A manifest stuck on "streaming" (crash mid-run) should read idle.
        ids = active_chat_ids if active_chat_ids is not None else _active_chat_ids()
        if chat.id not in ids:
            return "idle"
    return status


def _session_out(chat: ChatMetadata, active_chat_ids: set[str] | None = None) -> SessionOut:
    return SessionOut(
        id=chat.id,
        hermes_session_id=chat.id,
        title=chat.title,
        status=_gateway_status(chat, active_chat_ids),
        created_at=ms_to_iso_z(chat.createdAt),
        updated_at=ms_to_iso_z(chat.updatedAt),
    )


def _assistant_text(message: AssistantMessage) -> str:
    return "".join(event.value for event in message.events if event.type == "token")


def _chat_to_gateway_messages(session_id: str) -> list[MessageOut]:
    rows: list[MessageOut] = []
    for index, message in enumerate(load_chat_session(session_id)):
        if isinstance(message, UserMessage):
            content: str = message.content
            metadata: dict[str, Any] = {}
        else:
            content = _assistant_text(message)
            metadata = {"run_id": message.runId, "status": message.status}
        rows.append(
            MessageOut(
                id=index + 1,
                session_id=session_id,
                role=message.role,
                content=content,
                metadata=metadata,
                created_at=ms_to_iso_z(message.createdAt),
            )
        )
    return [row for row in rows if row.content]


@router.post(
    "/sessions",
    response_model=SessionOut,
    dependencies=[Depends(require_accepting_work)],
)
async def create_session(body: SessionCreate) -> SessionOut:
    chat_id = str(uuid.uuid4())
    save_chat_session(chat_id, [])
    if body.title:
        update_chat_title(chat_id, body.title)
    get_gateway_bus().publish(chat_id, "session.created", {"title": body.title})
    return _session_out(_get_chat_or_404(chat_id))


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions() -> list[SessionOut]:
    active_chat_ids = _active_chat_ids()
    return [_session_out(chat, active_chat_ids) for chat in list_chat_history()]


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(session_id: str) -> SessionOut:
    return _session_out(_get_chat_or_404(session_id))


@router.get("/sessions/{session_id}/messages", response_model=list[MessageOut])
async def get_messages(session_id: str) -> list[MessageOut]:
    _get_chat_or_404(session_id)
    return _chat_to_gateway_messages(session_id)


@router.post(
    "/sessions/{session_id}/messages",
    response_model=MessageSubmitOut,
    dependencies=[Depends(require_accepting_work)],
)
async def submit_message(session_id: str, body: MessageCreate) -> MessageSubmitOut:
    _get_chat_or_404(session_id)

    now = int(time.time() * 1000)
    user_message = UserMessage(
        id=str(uuid.uuid4()),
        createdAt=now,
        updatedAt=now,
        status="complete",
        content=body.content,
    )
    save_chat_session(session_id, [*load_chat_session(session_id), user_message])
    update_chat_status(session_id, "streaming")

    # Published before submit_run so it precedes assistant.started in id order.
    get_gateway_bus().publish(session_id, "user.message", {"text": body.content})
    await get_runs_service().submit_run(RunCreateRequest(kind="chat", chatId=session_id))

    return MessageSubmitOut(status="accepted", session_id=session_id, hermes=None)


@router.get("/sessions/{session_id}/events/history", response_model=list[EventOut])
async def get_event_history(session_id: str, after: int = 0, limit: int = 200) -> list[EventOut]:
    _get_chat_or_404(session_id)
    rows = list_gateway_events_after(session_id, after=after, limit=limit)
    return [EventOut(**row) for row in rows]


def _format_sse(row: dict[str, Any]) -> str:
    return f"id: {row['id']}\nevent: {row['type']}\ndata: {json.dumps(row)}\n\n"


@router.get("/sessions/{session_id}/events")
async def stream_events(
    session_id: str,
    after: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    _get_chat_or_404(session_id)

    try:
        initial_cursor = int(last_event_id) if last_event_id is not None else int(after)
    except ValueError:
        initial_cursor = int(after)

    bus = get_gateway_bus()

    async def event_source() -> AsyncIterator[str]:
        cursor = initial_cursor
        # Subscribe before the replay query: a concurrently published event is
        # either caught by the query (then dropped from the queue by the
        # id <= cursor check) or missed by the query but waiting in the queue.
        queue = bus.subscribe(session_id)
        try:
            for row in list_gateway_events_after(session_id, after=cursor):
                cursor = row["id"]
                yield _format_sse(row)
            while True:
                try:
                    row = await asyncio.wait_for(queue.get(), timeout=_SSE_KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if row["id"] <= cursor:
                    continue
                cursor = row["id"]
                yield _format_sse(row)
        finally:
            bus.unsubscribe(session_id, queue)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str) -> dict[str, Any]:
    _get_chat_or_404(session_id)

    runs_service = get_runs_service()
    active = runs_service.list_active_runs(kinds=["chat"])
    target = next((snapshot for snapshot in active if snapshot.chatId == session_id), None)
    stopped_jobs = await asyncio.to_thread(
        codex_job_manager.stop_for_session, session_id
    )
    if target is None:
        return {
            "status": "interrupted" if stopped_jobs else "idle",
            "hermes": None,
        }

    stopped = await runs_service.stop_run(target.runId)
    return {"status": "interrupted" if stopped else "idle", "hermes": None}


@router.get("/sessions/{session_id}/codex-jobs/{job_id}")
async def get_codex_job(session_id: str, job_id: str) -> dict[str, Any]:
    _get_chat_or_404(session_id)
    result = codex_job_manager.poll(job_id, session_id=session_id)
    if "error" in result and result.get("status") is None:
        raise HTTPException(status_code=404, detail="Codex job not found for this session")
    return result


@router.get("/sessions/{session_id}/codex-jobs")
async def list_codex_jobs(session_id: str) -> list[dict[str, Any]]:
    _get_chat_or_404(session_id)
    return codex_job_manager.list_for_session(session_id)


@router.post("/sessions/{session_id}/codex-jobs/{job_id}/answers")
async def answer_codex_job(
    session_id: str, job_id: str, body: CodexAnswers
) -> dict[str, Any]:
    _get_chat_or_404(session_id)
    result = await asyncio.to_thread(
        codex_job_manager.answer, job_id, body.answers, session_id
    )
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@router.post("/sessions/{session_id}/codex-jobs/{job_id}/cancel")
async def cancel_codex_job(session_id: str, job_id: str) -> dict[str, Any]:
    _get_chat_or_404(session_id)
    result = await asyncio.to_thread(
        codex_job_manager.stop, job_id, session_id
    )
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/codex-metrics")
async def get_codex_metrics() -> dict[str, Any]:
    return codex_job_manager.metrics()
