from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from .chat import Chat

WSEnvelopeTypes = Literal["chat"]


class WSEnvelope(BaseModel):
    type: WSEnvelopeTypes
    data: Any


class ChatWSEnvelope(WSEnvelope):
    type: Literal["chat"] = "chat"
    data: Chat
