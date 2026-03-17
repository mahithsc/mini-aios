from __future__ import annotations

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from server.ws.manager import connection_manager
from server.ws.router import parse_ws_envelope, router

async def handle_websocket_connection(websocket: WebSocket) -> None:
    connection = await connection_manager.connect(websocket)

    try:
        while True:
            try:
                envelope = parse_ws_envelope(await websocket.receive_json())
                print(envelope)
            except ValidationError:
                await websocket.close(code=1003, reason="Invalid websocket envelope.")
                return
            
            async for event in router(envelope):
                await connection_manager.send(connection.id, event)
    except WebSocketDisconnect:
        return
    finally:
        await connection_manager.disconnect(connection.id)
