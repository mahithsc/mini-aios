from __future__ import annotations

from server.types.notification import Notification


class NotificationBroadcaster:
    """Notifications are persisted by NotificationService; live push was
    WebSocket-only and died with that transport. Future: publish on a
    dedicated gateway SSE notifications channel."""

    async def broadcast_created(self, notification: Notification) -> None:
        return

    async def broadcast_dismissed(self, notification: Notification) -> None:
        return
