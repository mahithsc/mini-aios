from __future__ import annotations

import os
import socket
import traceback
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from aios_core.db import clear_device_link, get_device_link, get_or_create_device_id
from aios_core.initialize import (
    register_runtime_shutdown,
    shutdown_runtime,
    start_runtime,
)
from aios_core.sessions import list_chat_history, load_chat_session
from server.apps import router as apps_router
from server.auth import require_local_token
from server.commands import handle_device_command
from server.discovery import AiosDiscovery
from server.execution.runtime import shutdown_runs_service, start_runs_service
from server.gateway.routes import router as gateway_router
from server.integrations.doordash import router as doordash_integration_router
from server.integrations.gmail import router as gmail_integration_router
from server.integrations.google import router as google_integration_router
from server.message import stream_message
from server.notifications.runtime import (
    get_notification_service,
    shutdown_notification_service,
    start_notification_service,
)
from server.pairing import PairingError, complete_pairing
from server.relay_client import relay_client
from server.tunnel import start_if_paired, stop_cloudflared
from server.types.chat import Chat
from server.uploads import save_uploads
from server.updater import require_accepting_work, router as updater_router

register_runtime_shutdown()

_TRUTHY = {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_runtime(start_crons=False)
    await start_notification_service()
    await start_runs_service()

    app.state.device_id = get_or_create_device_id()
    app.state.discovery = None
    if os.getenv("AIOS_DISABLE_MDNS", "").strip().lower() not in _TRUTHY:
        discovery = AiosDiscovery(
            device_id=app.state.device_id,
            port=int(os.getenv("AIOS_SERVER_PORT", "8765")),
        )
        try:
            await discovery.start()
            app.state.discovery = discovery
        except Exception as exc:  # advertising is best-effort; never block startup
            # Log the type + repr (some zeroconf errors have an empty str) plus a
            # traceback, so a silent advertise failure is actually debuggable.
            print(f"[discovery] mDNS advertise failed: {type(exc).__name__}: {exc!r}")
            traceback.print_exc()

    # Public Cloudflare Tunnel so the desktop can reach this box when off-LAN.
    # Starts only if already paired (has a connector token); otherwise it's
    # started right after pairing. Best-effort — never blocks startup.
    if os.getenv("AIOS_DISABLE_TUNNEL", "").strip().lower() not in _TRUTHY:
        try:
            start_if_paired()
        except Exception as exc:
            print(f"[tunnel] failed to start: {exc}")

    # Outbound relay to the cloud. Self-activates once paired, so it's safe to
    # start unconditionally (and picks up pairing that happens at runtime).
    relay_client.start()

    try:
        yield
    finally:
        await relay_client.stop()
        stop_cloudflared()
        if app.state.discovery is not None:
            await app.state.discovery.stop()
        await shutdown_notification_service()
        await shutdown_runs_service()
        shutdown_runtime()


app = FastAPI(lifespan=lifespan)

# The desktop app talks to the box from its main process (Node fetch / WS),
# which is not subject to CORS, so we no longer need a wildcard. Restrict to
# localhost dev origins (overridable) to avoid a browser being coaxed into
# driving the box. Access control proper is the local_token below.
_cors_origins = [
    o.strip()
    for o in os.getenv(
        "AIOS_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",")
    if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# AIOS gateway (/sessions ...) — the session/agent-run API the mobile + desktop
# chat clients speak. Auth is gated by AIOS_GATEWAY_TOKEN (open when unset).
app.include_router(gateway_router)
app.include_router(apps_router)
app.include_router(doordash_integration_router)
app.include_router(gmail_integration_router)
app.include_router(google_integration_router)
app.include_router(updater_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/device/info")
async def device_info() -> dict[str, object]:
    """Identity + pairing status. Public (needed for LAN discovery before a
    token exists) — so it deliberately omits owner PII, since once paired the
    box is reachable on a public subdomain."""
    link = get_device_link()
    return {
        "device_id": app.state.device_id,
        "name": os.getenv("AIOS_DEVICE_NAME") or socket.gethostname(),
        "paired": link is not None,
        "slug": link["slug"] if link else None,
    }


class PairRequest(BaseModel):
    pairing_code: str


@app.post("/pair")
async def pair(body: PairRequest) -> dict[str, object]:
    """Redeem a pairing code (handed over the LAN by the authenticated desktop
    app) with the cloud, binding this box to the user's account."""
    try:
        return await complete_pairing(body.pairing_code)
    except PairingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


class CommandRequest(BaseModel):
    type: str
    payload: dict | None = None


@app.post("/unpair", dependencies=[Depends(require_local_token)])
async def unpair() -> dict[str, object]:
    """Forget this box's account binding. Clears the local link and restarts the
    relay client so its held socket drops and it goes idle until re-paired."""
    clear_device_link()
    stop_cloudflared()
    await relay_client.stop()
    relay_client.start()
    return {"status": "unpaired"}


@app.post("/command", dependencies=[Depends(require_local_token)])
async def command(body: CommandRequest) -> dict[str, object]:
    """Direct LAN command path (token-guarded). The relay client uses the same
    `handle_device_command` for the off-LAN path."""
    try:
        return {"ok": True, "result": handle_device_command(body.type, body.payload or {})}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class MessageRequest(BaseModel):
    chat: Chat
    turnId: str | None = None


@app.post("/message", dependencies=[Depends(require_local_token)])
async def message(body: MessageRequest) -> StreamingResponse:
    """Send a user turn and stream the agent's run events as SSE. HTTP
    request/response equivalent of the `/ws` chat path."""
    await require_accepting_work()
    return StreamingResponse(
        stream_message(body.chat, body.turnId),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/chats", dependencies=[Depends(require_local_token)])
async def list_chats() -> list[dict[str, object]]:
    """Chat-history list. HTTP equivalent of the `/ws` `chat-history` (no id)."""
    return [chat.model_dump(mode="json") for chat in list_chat_history()]


@app.get("/chats/{chat_id}", dependencies=[Depends(require_local_token)])
async def get_chat(chat_id: str) -> dict[str, object]:
    """A single chat with its messages. HTTP equivalent of `chat-history` by id."""
    meta = next((chat for chat in list_chat_history() if chat.id == chat_id), None)
    if meta is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return Chat(
        id=meta.id,
        title=meta.title,
        createdAt=meta.createdAt,
        updatedAt=meta.updatedAt,
        status=meta.status,
        messages=load_chat_session(chat_id),
    ).model_dump(mode="json")


@app.get("/notifications", dependencies=[Depends(require_local_token)])
async def list_notifications() -> dict[str, object]:
    """Notification list. HTTP equivalent of the `/ws` `notification.list`.
    The desktop polls this to surface new notifications (replaces the WS push)."""
    return get_notification_service().list_notifications().model_dump(mode="json")


@app.post("/notifications/{notification_id}/dismiss", dependencies=[Depends(require_local_token)])
async def dismiss_notification(notification_id: str) -> dict[str, object]:
    """Dismiss a notification and return it. HTTP equivalent of
    `notification.dismiss`; the desktop uses the returned notification to remove
    it from its store (mirrors the old `/ws` dismiss echo)."""
    dismissed = get_notification_service().dismiss_notification(notification_id)
    if dismissed is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    return dismissed.model_dump(mode="json")


@app.get("/crons/upcoming", dependencies=[Depends(require_local_token)])
async def list_upcoming_crons() -> dict[str, object]:
    """Upcoming crons. HTTP equivalent of the `/ws` `cron.upcoming.list`, which
    was never handled server-side, so this preserves that (empty) behavior."""
    return {"crons": []}


@app.post("/attachments", dependencies=[Depends(require_local_token)])
async def upload_attachments(
    chatId: str = Form(...),
    files: list[UploadFile] = File(...),
) -> dict[str, object]:
    attachments = await save_uploads(chatId, files)
    return {"attachments": [attachment.model_dump(mode="json") for attachment in attachments]}
