from __future__ import annotations

import asyncio
import logging
from typing import Any

from aios_core.db import DB_PATH
from server.types.run import Run, RunEvent

from .store import insert_gateway_event
from .translate import ChatRunEventTranslator

log = logging.getLogger(__name__)


class GatewayEventBus:
    """Persist gateway events and fan them out to live SSE subscribers.

    `publish` is deliberately synchronous (no awaits): the INSERT assigns the
    event id and the fan-out delivers it in one uninterrupted event-loop step,
    so ids seen by any subscriber queue are strictly increasing. The SSE
    route's subscribe-then-replay dedupe relies on this.
    """

    def __init__(self, *, db_path: str = DB_PATH) -> None:
        self._db_path = db_path
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._translator = ChatRunEventTranslator()

    async def handle_run_accepted(self, run: Run) -> None:
        return

    async def handle_run_event(self, event: RunEvent) -> None:
        for event_type, payload in self._translator.translate(event):
            try:
                self.publish(event.chatId or "", event_type, payload)
            except Exception:
                log.exception("Failed to publish gateway event %s", event_type)

    def publish(self, session_id: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        row = insert_gateway_event(session_id, event_type, payload, db_path=self._db_path)
        for queue in list(self._subscribers.get(session_id, ())):
            queue.put_nowait(row)
        return row

    def subscribe(self, session_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.setdefault(session_id, set()).add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        queues = self._subscribers.get(session_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            self._subscribers.pop(session_id, None)


_gateway_bus = GatewayEventBus()


def get_gateway_bus() -> GatewayEventBus:
    return _gateway_bus
