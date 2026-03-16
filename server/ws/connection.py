from __future__ import annotations

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from server.ws.router import parse_ws_envelope

async def handle_websocket_connection(websocket: WebSocket) -> None:
    await websocket.accept()

    try:
        while True:
            try:
                envelope = parse_ws_envelope(await websocket.receive_json())
            except ValidationError:
                await websocket.close(code=1003, reason="Invalid websocket envelope.")
                return

            await websocket.send_json(envelope.model_dump(mode="json"))
    except WebSocketDisconnect:
        return
