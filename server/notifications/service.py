from __future__ import annotations

import asyncio
import time
import uuid

from aios_core.db import DB_PATH, get_db_connection
from server.notifications.broadcaster import NotificationBroadcaster
from server.types.notification import Notification, NotificationListResponse, NotificationSource

DEFAULT_NOTIFICATION_LOOKBACK_DAYS = 7


class NotificationService:
    def __init__(
        self,
        *,
        db_path: str = DB_PATH,
        broadcaster: NotificationBroadcaster,
    ) -> None:
        self._db_path = db_path
        self._broadcaster = broadcaster

    async def start(self) -> None:
        return

    async def shutdown(self) -> None:
        return

    def create_notification(
        self,
        *,
        source: NotificationSource,
        title: str,
        body: str,
        level: str = "info",
        source_id: str | None = None,
        run_id: str | None = None,
        chat_id: str | None = None,
    ) -> Notification:
        now = int(time.time() * 1000)
        notification = Notification(
            id=str(uuid.uuid4()),
            source=source,
            sourceId=source_id,
            runId=run_id,
            chatId=chat_id,
            level=level,
            title=title,
            body=body,
            createdAt=now,
            updatedAt=now,
            dismissedAt=None,
        )

        with get_db_connection(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO notifications (
                    id, source, source_id, run_id, chat_id, level,
                    title, body, created_at, updated_at, dismissed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification.id,
                    notification.source,
                    notification.sourceId,
                    notification.runId,
                    notification.chatId,
                    notification.level,
                    notification.title,
                    notification.body,
                    notification.createdAt,
                    notification.updatedAt,
                    notification.dismissedAt,
                ),
            )

        self._broadcast(self._broadcaster.broadcast_created(notification))
        return notification

    def list_notifications(
        self,
        *,
        lookback_days: int = DEFAULT_NOTIFICATION_LOOKBACK_DAYS,
    ) -> NotificationListResponse:
        threshold_ms = int(time.time() * 1000) - max(0, lookback_days) * 24 * 60 * 60 * 1000
        with get_db_connection(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, source, source_id, run_id, chat_id, level,
                       title, body, created_at, updated_at, dismissed_at
                FROM notifications
                WHERE dismissed_at IS NULL AND created_at >= ?
                ORDER BY created_at DESC
                """,
                (threshold_ms,),
            ).fetchall()

        return NotificationListResponse(
            notifications=[self._row_to_notification(row) for row in rows]
        )

    def dismiss_notification(self, notification_id: str) -> Notification | None:
        existing = self.get_notification(notification_id)
        if existing is None:
            return None

        dismissed_at = existing.dismissedAt or int(time.time() * 1000)
        updated = existing.model_copy(
            update={
                "updatedAt": dismissed_at,
                "dismissedAt": dismissed_at,
            }
        )

        with get_db_connection(self._db_path) as conn:
            conn.execute(
                """
                UPDATE notifications
                SET updated_at = ?, dismissed_at = ?
                WHERE id = ?
                """,
                (updated.updatedAt, updated.dismissedAt, notification_id),
            )

        self._broadcast(self._broadcaster.broadcast_dismissed(updated))
        return updated

    def get_notification(self, notification_id: str) -> Notification | None:
        with get_db_connection(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT id, source, source_id, run_id, chat_id, level,
                       title, body, created_at, updated_at, dismissed_at
                FROM notifications
                WHERE id = ?
                """,
                (notification_id,),
            ).fetchone()

        if row is None:
            return None

        return self._row_to_notification(row)

    @staticmethod
    def _row_to_notification(row: tuple[object, ...]) -> Notification:
        return Notification(
            id=str(row[0]),
            source=row[1],
            sourceId=row[2],
            runId=row[3],
            chatId=row[4],
            level=row[5],
            title=str(row[6]),
            body=str(row[7]),
            createdAt=int(row[8]),
            updatedAt=int(row[9]),
            dismissedAt=int(row[10]) if row[10] is not None else None,
        )

    @staticmethod
    def _broadcast(coroutine) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coroutine)
            return

        loop.create_task(coroutine)
