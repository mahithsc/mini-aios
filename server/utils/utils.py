from __future__ import annotations

from pydantic import TypeAdapter

from server.types.chat import AssistantMessage, Chat, ChatMessage, OpenAIMessage, UserMessage
from server.types.ws import WSEnvelope

CHAT_MESSAGE_ADAPTER = TypeAdapter(ChatMessage)


def _to_openai_message(message: ChatMessage) -> OpenAIMessage:
    if isinstance(message, UserMessage):
        return OpenAIMessage(role="user", content=message.content)

    if isinstance(message, AssistantMessage):
        content = "".join(
            event.value for event in message.events if getattr(event, "type", None) == "token"
        )
        return OpenAIMessage(role="assistant", content=content)

    raise TypeError(f"Unsupported chat message type: {type(message)!r}")


def format_chat_messages_to_openai_messages(messages: list[ChatMessage]) -> list[OpenAIMessage]:
    return [_to_openai_message(CHAT_MESSAGE_ADAPTER.validate_python(message)) for message in messages]


def format_from_envelope_to_messages(envelope: WSEnvelope) -> list[OpenAIMessage]:
    chat_data = envelope.data if isinstance(envelope.data, Chat) else Chat.model_validate(envelope.data)
    print("Chat Data ------->", chat_data)
    return format_chat_messages_to_openai_messages(chat_data.messages)