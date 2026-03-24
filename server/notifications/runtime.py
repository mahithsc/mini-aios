from __future__ import annotations

from server.notifications.broadcaster import NotificationBroadcaster
from server.notifications.service import NotificationService

_notification_service: NotificationService | None = None


def initialize_notification_service() -> NotificationService:
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService(
            broadcaster=NotificationBroadcaster(),
        )
    return _notification_service


def get_notification_service() -> NotificationService:
    if _notification_service is None:
        raise RuntimeError("NotificationService has not been initialized.")
    return _notification_service


async def start_notification_service() -> NotificationService:
    service = initialize_notification_service()
    await service.start()
    return service


async def shutdown_notification_service() -> None:
    global _notification_service
    if _notification_service is None:
        return
    await _notification_service.shutdown()
    _notification_service = None
