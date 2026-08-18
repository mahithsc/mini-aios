from __future__ import annotations

import asyncio
import threading

from server.types.notification import Notification


class NotificationBroadcaster:
    """Fan persisted notification changes out to live gateway subscribers."""

    def __init__(self, *, queue_size: int = 256) -> None:
        self._subscribers: dict[
            asyncio.Queue[dict[str, object]], asyncio.AbstractEventLoop
        ] = {}
        self._subscribers_lock = threading.Lock()
        self._queue_size = max(1, queue_size)

    async def broadcast_created(self, notification: Notification) -> None:
        self._publish("notification.created", notification)

    async def broadcast_dismissed(self, notification: Notification) -> None:
        self._publish("notification.dismissed", notification)

    def subscribe(self) -> asyncio.Queue[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(
            maxsize=self._queue_size
        )
        loop = asyncio.get_running_loop()
        with self._subscribers_lock:
            self._subscribers[queue] = loop
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, object]]) -> None:
        with self._subscribers_lock:
            self._subscribers.pop(queue, None)

    def _publish(self, event_type: str, notification: Notification) -> None:
        message: dict[str, object] = {
            "type": event_type,
            "notification": notification.model_dump(mode="json"),
        }
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        with self._subscribers_lock:
            subscribers = list(self._subscribers.items())
        for queue, subscriber_loop in subscribers:
            if subscriber_loop is current_loop:
                self._enqueue_latest(queue, message)
                continue
            try:
                subscriber_loop.call_soon_threadsafe(
                    self._enqueue_latest, queue, message
                )
            except RuntimeError:
                self.unsubscribe(queue)

    @staticmethod
    def _enqueue_latest(
        queue: asyncio.Queue[dict[str, object]], message: dict[str, object]
    ) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            pass
