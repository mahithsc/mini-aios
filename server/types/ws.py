from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from .chat import Chat, ChatMetadata
from .cron import CronUpcomingListResponse
from .notification import Notification, NotificationDismissRequest, NotificationListResponse
from .run import ProcessSnapshotListRequest, Run, RunEvent, RunResumeRequest, RunSnapshot

WSEnvelopeTypes = Literal[
    "chat",
    "chat-history",
    "chat.submit",
    "cron.upcoming.list",
    "notification.list",
    "notification.created",
    "notification.dismiss",
    "run.accepted",
    "run.event",
    "process.snapshot.list",
    "run.resume",
]


class WSEnvelope(BaseModel):
    type: WSEnvelopeTypes
    data: Any


class ChatWSEnvelope(WSEnvelope):
    type: Literal["chat"] = "chat"
    data: Chat


class ChatHistoryWSEnvelope(WSEnvelope):
    type: Literal["chat-history"] = "chat-history"
    data: list[ChatMetadata] | Chat | str | None = None


class ChatSubmitWSEnvelope(WSEnvelope):
    type: Literal["chat.submit"] = "chat.submit"
    data: dict[str, Any] | Chat


class CronUpcomingListWSEnvelope(WSEnvelope):
    type: Literal["cron.upcoming.list"] = "cron.upcoming.list"
    data: CronUpcomingListResponse | None = None


class NotificationListWSEnvelope(WSEnvelope):
    type: Literal["notification.list"] = "notification.list"
    data: NotificationListResponse | None = None


class NotificationCreatedWSEnvelope(WSEnvelope):
    type: Literal["notification.created"] = "notification.created"
    data: Notification


class NotificationDismissWSEnvelope(WSEnvelope):
    type: Literal["notification.dismiss"] = "notification.dismiss"
    data: NotificationDismissRequest | Notification


class RunAcceptedWSEnvelope(WSEnvelope):
    type: Literal["run.accepted"] = "run.accepted"
    data: Run


class RunEventWSEnvelope(WSEnvelope):
    type: Literal["run.event"] = "run.event"
    data: RunEvent


class ProcessSnapshotListWSEnvelope(WSEnvelope):
    type: Literal["process.snapshot.list"] = "process.snapshot.list"
    data: ProcessSnapshotListRequest | list[RunSnapshot] | None = None


class RunResumeWSEnvelope(WSEnvelope):
    type: Literal["run.resume"] = "run.resume"
    data: RunResumeRequest | list[RunEvent]
