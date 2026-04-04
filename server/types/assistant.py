from __future__ import annotations

from pydantic import BaseModel

from .chat import UnixMs


class Assistant(BaseModel):
    id: str
    chatId: str
    title: str
    createdAt: UnixMs
    updatedAt: UnixMs
    heartbeatEnabled: bool = False
    identityPath: str
    heartbeatPath: str
    memoryPath: str


class AssistantInitRequest(BaseModel):
    chatId: str
    title: str | None = None
    identityBody: str | None = None
    heartbeatBody: str | None = None
    memoryBody: str | None = None
