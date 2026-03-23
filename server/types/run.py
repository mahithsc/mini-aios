from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .chat import UnixMs

RunKind = Literal["chat", "cron", "heartbeat"]
RunStatus = Literal["queued", "running", "completed", "error"]
RunEventType = Literal[
    "started",
    "token",
    "tool_call_start",
    "tool_call_end",
    "progress",
    "completed",
    "error",
]


class Run(BaseModel):
    id: str
    kind: RunKind
    status: RunStatus
    createdAt: UnixMs
    updatedAt: UnixMs
    chatId: str | None = None
    sourceId: str | None = None
    turnId: str | None = None


class RunCreateRequest(BaseModel):
    kind: RunKind
    chatId: str | None = None
    sourceId: str | None = None
    turnId: str | None = None


class RunEventPayload(BaseModel):
    type: RunEventType
    data: dict[str, Any] | None = None


class RunEvent(BaseModel):
    runId: str
    sequence: int
    createdAt: UnixMs
    chatId: str | None = None
    event: RunEventPayload


class RunSnapshot(BaseModel):
    runId: str
    kind: RunKind
    status: RunStatus
    updatedAt: UnixMs
    chatId: str | None = None
    lastSequence: int
    preview: str | None = None
    activeStep: str | None = None


class ProcessSnapshotListRequest(BaseModel):
    statuses: list[RunStatus] = Field(default_factory=list)


class RunResumeRequest(BaseModel):
    runId: str
    afterSequence: int
