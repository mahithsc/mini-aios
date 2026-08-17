from __future__ import annotations

import asyncio

from server.types.notification import Notification


class NotificationBroadcaster:
    """Fan persisted notification changes out to live gateway subscribers."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict]] = set()

    async def broadcast_created(self, notification: Notification) -> None:
        self._publish("notification.created", notification)

    async def broadcast_dismissed(self, notification: Notification) -> None:
        self._publish("notification.dismissed", notification)

    def subscribe(self) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict]) -> None:
        self._subscribers.discard(queue)

    def _publish(self, event_type: str, notification: Notification) -> None:
        message = {
            "type": event_type,
            "notification": notification.model_dump(mode="json"),
        }
        for queue in list(self._subscribers):
            queue.put_nowait(message)
