from __future__ import annotations

import logging

from fastapi import WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError

from server.auth import AuthError, get_user_from_token
from server.users import ensure_profile
from server.ws.manager import connection_manager
from server.ws.router import parse_ws_envelope, router

log = logging.getLogger(__name__)


async def handle_websocket_connection(websocket: WebSocket) -> None:
    try:
        user = get_user_from_token(websocket.query_params.get("access_token"))
    except AuthError as exc:
        log.warning("Websocket authentication failed: %s", exc)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=str(exc))
        return

    try:
        ensure_profile(user)
    except Exception:
        log.exception("Failed to initialize profile for user %s.", user.id)
        await websocket.close(code=1011, reason="Failed to initialize user profile.")
        return

    connection = await connection_manager.connect(
        websocket,
        user_id=user.id,
        email=user.email,
    )

    try:
        while True:
            try:
                envelope = parse_ws_envelope(await websocket.receive_json())
            except ValidationError:
                await websocket.close(code=1003, reason="Invalid websocket envelope.")
                return

            async for event in router(envelope, user):
                await connection_manager.send(connection.id, event)
    except WebSocketDisconnect:
        return
    finally:
        await connection_manager.disconnect(connection.id)
