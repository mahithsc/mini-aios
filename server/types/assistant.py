from __future__ import annotations

from pydantic import BaseModel

from .chat import ChatMessage, UnixMs


class Assistant(BaseModel):
    id: str
    title: str
    createdAt: UnixMs
    updatedAt: UnixMs
    heartbeatEnabled: bool = False
    identityPath: str
    heartbeatPath: str
    memoryPath: str


class AssistantDetail(Assistant):
    messages: list[ChatMessage]


class AssistantCreateRequest(BaseModel):
    id: str
    title: str | None = None
    prompt: str


class AssistantSubmitRequest(BaseModel):
    assistantId: str
    messages: list[ChatMessage]
    turnId: str | None = None
