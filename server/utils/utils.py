from __future__ import annotations

import json

from pydantic import TypeAdapter

from server.types.chat import AssistantMessage, Chat, ChatMessage, OpenAIMessage, UserMessage
from server.types.ws import WSEnvelope

CHAT_MESSAGE_ADAPTER = TypeAdapter(ChatMessage)

# Cap tool args/results when embedding in plain-text assistant content for the LLM.
_MAX_TOOL_PAYLOAD_CHARS = 8_000


def _serialize_tool_payload(payload: object) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        text = payload
    else:
        try:
            text = json.dumps(payload, indent=2, default=str)
        except TypeError:
            text = str(payload)
    if len(text) > _MAX_TOOL_PAYLOAD_CHARS:
        return f"{text[:_MAX_TOOL_PAYLOAD_CHARS]}\n... (truncated for context)"
    return text


def _assistant_events_to_openai_content(message: AssistantMessage) -> str:
    """Flatten assistant transcript (tokens + tool lifecycle) into one string.

    Native tool-message APIs could be used later; this keeps a single
    user/assistant channel compatible with the current Agent setup.
    """
    parts: list[str] = []
    for event in message.events:
        etype = event.type
        if etype == "token":
            parts.append(event.value)
        elif etype == "tool_call_start":
            args = _serialize_tool_payload(event.input)
            parts.append(
                f"\n\n[Tool call: {event.toolName} id={event.toolCallId}]\n{args}\n"
            )
        elif etype == "tool_call_end":
            out = _serialize_tool_payload(event.output)
            parts.append(
                f"\n[Tool result: {event.toolName} id={event.toolCallId}]\n{out}\n"
            )
        elif etype == "tool_call_error":
            parts.append(
                f"\n[Tool error: {event.toolName} id={event.toolCallId}]\n{event.error}\n"
            )
        elif etype == "stream_error":
            parts.append(f"\n[Stream error]\n{event.error}\n")
        elif etype in ("stream_start", "stream_end"):
            continue
        else:
            continue
    return "".join(parts)


def _to_openai_message(message: ChatMessage) -> OpenAIMessage:
    if isinstance(message, UserMessage):
        return OpenAIMessage(role="user", content=message.content)

    if isinstance(message, AssistantMessage):
        return OpenAIMessage(
            role="assistant",
            content=_assistant_events_to_openai_content(message),
        )

    raise TypeError(f"Unsupported chat message type: {type(message)!r}")


def format_chat_messages_to_openai_messages(messages: list[ChatMessage]) -> list[OpenAIMessage]:
    return [_to_openai_message(CHAT_MESSAGE_ADAPTER.validate_python(message)) for message in messages]


def format_from_envelope_to_messages(envelope: WSEnvelope) -> list[OpenAIMessage]:
    chat_data = envelope.data if isinstance(envelope.data, Chat) else Chat.model_validate(envelope.data)
    print("Chat Data ------->", chat_data)
    return format_chat_messages_to_openai_messages(chat_data.messages)