from __future__ import annotations

import os
import socket
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from aios_core.db import clear_device_link, get_device_link, get_or_create_device_id
from aios_core.initialize import register_runtime_shutdown, shutdown_runtime, start_runtime
from server.auth import is_valid_ws_token, require_local_token
from server.commands import handle_device_command
from server.discovery import AiosDiscovery
from server.pairing import PairingError, complete_pairing
from server.relay_client import relay_client
from server.tunnel import start_tunnel, stop_tunnel
from server.notifications.runtime import shutdown_notification_service, start_notification_service
from server.execution.runtime import shutdown_runs_service, start_runs_service
from server.uploads import save_uploads
from server.ws.connection import handle_websocket_connection

register_runtime_shutdown()

_TRUTHY = {"1", "true", "yes", "on"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    start_runtime(start_heartbeat=False)
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
            print(f"[discovery] mDNS advertise failed: {exc}")

    # Public tunnel so the desktop can reach this box directly when off-LAN.
    if os.getenv("AIOS_DISABLE_TUNNEL", "").strip().lower() not in _TRUTHY:
        port = int(os.getenv("AIOS_SERVER_PORT", "8765"))
        try:
            await start_tunnel(port)
        except Exception as exc:  # tunnel is best-effort; never block startup
            print(f"[tunnel] failed to start: {exc}")

    # Outbound relay to the cloud. Self-activates once paired, so it's safe to
    # start unconditionally (and picks up pairing that happens at runtime).
    relay_client.start()

    try:
        yield
    finally:
        await relay_client.stop()
        stop_tunnel()
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


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/device/info")
async def device_info() -> dict[str, object]:
    """Identity + pairing status of this physical box. Used by the desktop app
    to confirm which device it discovered and whether it's already claimed."""
    link = get_device_link()
    return {
        "device_id": app.state.device_id,
        "name": os.getenv("AIOS_DEVICE_NAME") or socket.gethostname(),
        "paired": link is not None,
        "owner_email": link["owner_email"] if link else None,
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


@app.post("/attachments", dependencies=[Depends(require_local_token)])
async def upload_attachments(
    chatId: str = Form(...),
    files: list[UploadFile] = File(...),
) -> dict[str, object]:
    attachments = await save_uploads(chatId, files)
    return {"attachments": [attachment.model_dump(mode="json") for attachment in attachments]}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    # Token arrives as a query param (?token=) since the WebSocket API can't
    # set headers. Reject before accepting the socket if it's missing/invalid.
    if not is_valid_ws_token(websocket.query_params.get("token")):
        await websocket.close(code=1008)
        return
    await handle_websocket_connection(websocket)
