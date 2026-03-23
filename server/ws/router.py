from __future__ import annotations

from collections.abc import AsyncIterator

from aios_core.sessions import list_chat_history, load_chat_session, save_chat_session, update_chat_status
from server.runs.runtime import get_runs_service
from server.types.chat import Chat, ChatMessage, UserMessage
from server.types.run import RunCreateRequest
from server.types.ws import WSEnvelope


def parse_ws_envelope(payload: object) -> WSEnvelope:
    return WSEnvelope.model_validate(payload)


def _get_latest_user_message(chat: Chat) -> UserMessage:
    for message in reversed(chat.messages):
        if isinstance(message, UserMessage):
            return message

    raise ValueError("Chat payload does not contain a user message.")


def _append_user_message(messages: list[ChatMessage], user_message: UserMessage) -> list[ChatMessage]:
    if messages and isinstance(messages[-1], UserMessage) and messages[-1].id == user_message.id:
        return messages

    return [*messages, user_message]

def _conversation_messages_for_turn(chat: Chat) -> list[ChatMessage]:
    """History + latest user turn to send to the model.

    The desktop client sends the full in-memory transcript (including assistant
    tool_call_* events). Older code only re-read disk + appended the latest user
    message, which dropped everything the client had for assistant turns.

    Prefer the client payload when it is at least as long as the persisted
    session so tool results and ordering stay aligned with the UI. If the
    client is shorter (e.g. not yet hydrated), fall back to disk + latest user.
    """
    persisted_messages = load_chat_session(chat.id)
    latest_user_message = _get_latest_user_message(chat)
    client_messages = list(chat.messages)

    if len(client_messages) >= len(persisted_messages):
        return client_messages

    return _append_user_message(persisted_messages, latest_user_message)


async def router(envelope: WSEnvelope) -> AsyncIterator[dict[str, object]]:
    if envelope.type == "chat-history":
        if isinstance(envelope.data, str):
            chat_id = envelope.data
            chat_history = next((chat for chat in list_chat_history() if chat.id == chat_id), None)

            if chat_history is None:
                return

            yield WSEnvelope(
                type="chat-history",
                data=Chat(
                    id=chat_history.id,
                    title=chat_history.title,
                    createdAt=chat_history.createdAt,
                    updatedAt=chat_history.updatedAt,
                    status=chat_history.status,
                    messages=load_chat_session(chat_id),
                ).model_dump(mode="json"),
            )
            return

        yield WSEnvelope(
            type="chat-history",
            data=[chat.model_dump(mode="json") for chat in list_chat_history()],
        )
        return

    if envelope.type in {"chat", "chat.submit"}:
        turn_id: str | None = None
        if envelope.type == "chat.submit" and isinstance(envelope.data, dict) and "chat" in envelope.data:
            chat = (
                envelope.data["chat"]
                if isinstance(envelope.data["chat"], Chat)
                else Chat.model_validate(envelope.data["chat"])
            )
            raw_turn_id = envelope.data.get("turnId")
            turn_id = raw_turn_id if isinstance(raw_turn_id, str) else None
        else:
            chat = envelope.data if isinstance(envelope.data, Chat) else Chat.model_validate(envelope.data)
        next_messages = _conversation_messages_for_turn(chat)
        save_chat_session(chat.id, next_messages)
        update_chat_status(chat.id, "streaming")
        run = await get_runs_service().submit_run(
            RunCreateRequest(
                kind="chat",
                chatId=chat.id,
                turnId=turn_id,
            )
        )
        yield WSEnvelope(
            type="run.accepted",
            data=run.model_dump(mode="json"),
        ) 