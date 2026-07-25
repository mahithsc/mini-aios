from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .chat import UnixMs

NotificationSource = Literal["chat", "cron", "system"]
NotificationLevel = Literal["info", "success", "warning", "error"]


class Notification(BaseModel):
    id: str
    source: NotificationSource
    sourceId: str | None = None
    runId: str | None = None
    chatId: str | None = None
    level: NotificationLevel
    title: str
    body: str
    createdAt: UnixMs
    updatedAt: UnixMs
    dismissedAt: UnixMs | None = None


class NotificationListResponse(BaseModel):
    notifications: list[Notification] = Field(default_factory=list)


class NotificationDismissRequest(BaseModel):
    id: str
