from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

UnixMs = int

ChatStatus = Literal["idle", "streaming", "error", "cancelled"]
MessageStatus = Literal["pending", "streaming", "complete", "error", "cancelled"]
AttachmentKind = Literal["image", "file", "audio"]


class MessageAttachment(BaseModel):
    id: str
    kind: AttachmentKind
    name: str
    filePath: str
    mimeType: str | None = None
    sizeBytes: int | None = None
    uploadedAt: UnixMs | None = None


class ChatMetadata(BaseModel):
    id: str
    title: str | None = None
    createdAt: UnixMs
    updatedAt: UnixMs
    status: ChatStatus | None = None


class BaseMessage(BaseModel):
    id: str
    createdAt: UnixMs
    updatedAt: UnixMs
    status: MessageStatus


class BaseLLMEvent(BaseModel):
    id: str
    createdAt: UnixMs


class StreamStartEvent(BaseLLMEvent):
    type: Literal["stream_start"] = "stream_start"


class TokenEvent(BaseLLMEvent):
    type: Literal["token"] = "token"
    value: str


class ToolCallStartEvent(BaseLLMEvent):
    type: Literal["tool_call_start"] = "tool_call_start"
    toolCallId: str
    toolName: str
    input: object | None = None


class ToolCallEndEvent(BaseLLMEvent):
    type: Literal["tool_call_end"] = "tool_call_end"
    toolCallId: str
    toolName: str
    output: object | None = None


class ToolCallErrorEvent(BaseLLMEvent):
    type: Literal["tool_call_error"] = "tool_call_error"
    toolCallId: str
    toolName: str
    error: str


class StreamEndEvent(BaseLLMEvent):
    type: Literal["stream_end"] = "stream_end"


class StreamErrorEvent(BaseLLMEvent):
    type: Literal["stream_error"] = "stream_error"
    error: str


class StreamCancelledEvent(BaseLLMEvent):
    type: Literal["stream_cancelled"] = "stream_cancelled"
    reason: str


class SubagentToolEvent(BaseLLMEvent):
    type: Literal["subagent_tool_event"] = "subagent_tool_event"
    parentToolCallId: str
    childRunId: str
    childEventType: Literal[
        "stream_start",
        "tool_call_start",
        "tool_call_end",
        "tool_call_error",
        "stream_end",
        "stream_error",
    ]
    toolCallId: str | None = None
    toolName: str | None = None
    input: object | None = None
    output: object | None = None
    error: str | None = None


LLMEvent = Annotated[
    StreamStartEvent
    | TokenEvent
    | ToolCallStartEvent
    | ToolCallEndEvent
    | ToolCallErrorEvent
    | StreamEndEvent
    | StreamErrorEvent
    | StreamCancelledEvent
    | SubagentToolEvent,
    Field(discriminator="type"),
]


class UserMessage(BaseMessage):
    role: Literal["user"] = "user"
    content: str
    attachments: list[MessageAttachment] = Field(default_factory=list)


class AssistantMessage(BaseMessage):
    role: Literal["assistant"] = "assistant"
    runId: str | None = None
    events: list[LLMEvent] = Field(default_factory=list)


ChatMessage = Annotated[UserMessage | AssistantMessage, Field(discriminator="role")]


class Chat(ChatMetadata):
    messages: list[ChatMessage] = Field(default_factory=list)




# api message formt

class OpenAIMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str

class OpenAIMessages(BaseModel):
    messages: list[OpenAIMessage]
