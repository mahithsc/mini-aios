from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from .chat import Chat, ChatMetadata

WSEnvelopeTypes = Literal["chat", "chat-history"]


class WSEnvelope(BaseModel):
    type: WSEnvelopeTypes
    data: Any


class ChatWSEnvelope(WSEnvelope):
    type: Literal["chat"] = "chat"
    data: Chat


class ChatHistoryWSEnvelope(WSEnvelope):
    type: Literal["chat-history"] = "chat-history"
    data: list[ChatMetadata] | Chat | str | None = None
