from __future__ import annotations

from server.types.notification import Notification
from server.types.ws import NotificationCreatedWSEnvelope, NotificationDismissWSEnvelope
from server.ws.manager import ConnectionManager, connection_manager


class NotificationBroadcaster:
    def __init__(self, manager: ConnectionManager = connection_manager) -> None:
        self._manager = manager

    async def broadcast_created(self, notification: Notification) -> None:
        await self._manager.broadcast(
            NotificationCreatedWSEnvelope(
                type="notification.created",
                data=notification,
            )
        )

    async def broadcast_dismissed(self, notification: Notification) -> None:
        await self._manager.broadcast(
            NotificationDismissWSEnvelope(
                type="notification.dismiss",
                data=notification,
            )
        )
