from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder


@dataclass(slots=True)
class ClientConnection:
    id: str
    websocket: WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, ClientConnection] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> ClientConnection:
        await websocket.accept()
        connection = ClientConnection(id=str(uuid.uuid4()), websocket=websocket)

        async with self._lock:
            self._connections[connection.id] = connection

        return connection

    async def disconnect(self, connection_id: str) -> None:
        async with self._lock:
            self._connections.pop(connection_id, None)

    async def send(self, connection_id: str, envelope: Any) -> bool:
        connection = await self._get_connection(connection_id)
        if connection is None:
            return False

        try:
            await connection.websocket.send_json(jsonable_encoder(envelope))
        except (RuntimeError, WebSocketDisconnect):
            await self.disconnect(connection_id)
            return False

        return True

    async def broadcast(self, envelope: Any) -> None:
        async with self._lock:
            connection_ids = list(self._connections.keys())

        for connection_id in connection_ids:
            await self.send(connection_id, envelope)

    async def _get_connection(self, connection_id: str) -> ClientConnection | None:
        async with self._lock:
            return self._connections.get(connection_id)


connection_manager = ConnectionManager()
