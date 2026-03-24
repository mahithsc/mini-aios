from __future__ import annotations

from server.notifications.runtime import get_notification_service
from server.types.notification import NotificationLevel, NotificationSource


def notify(
    title: str,
    body: str,
    level: NotificationLevel = "info",
    source: NotificationSource = "chat",
    source_id: str | None = None,
    run_id: str | None = None,
    chat_id: str | None = None,
):
    if not title or not title.strip():
        return "error: title is required"
    if not body or not body.strip():
        return "error: body is required"

    created = get_notification_service().create_notification(
        source=source,
        title=title.strip(),
        body=body.strip(),
        level=level,
        source_id=source_id,
        run_id=run_id,
        chat_id=chat_id,
    )
    return created
