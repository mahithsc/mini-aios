from __future__ import annotations

from fastapi import WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError

from server.auth import AuthError, get_user_from_token
from server.ws.manager import connection_manager
from server.ws.router import parse_ws_envelope, router


async def handle_websocket_connection(websocket: WebSocket) -> None:
    try:
        user = get_user_from_token(websocket.query_params.get("access_token"))
    except AuthError as exc:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=str(exc))
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

            async for event in router(envelope):
                await connection_manager.send(connection.id, event)
    except WebSocketDisconnect:
        return
    finally:
        await connection_manager.disconnect(connection.id)
