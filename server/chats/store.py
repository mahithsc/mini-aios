from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import TypeAdapter

from server.supabase import get_supabase_client
from server.types.chat import (
    AssistantMessage,
    Chat,
    ChatMessage,
    ChatMetadata,
    ChatStatus,
    LLMEvent,
    MessageAttachment,
    MessageStatus,
    UserMessage,
)

CHATS_TABLE = "chats"
CHAT_MESSAGES_TABLE = os.environ.get("SUPABASE_CHAT_MESSAGES_TABLE", "chat_messages")

CHAT_MESSAGE_ADAPTER = TypeAdapter(ChatMessage)
LLM_EVENT_ADAPTER = TypeAdapter(LLMEvent)

VALID_CHAT_STATUSES = {"idle", "streaming", "error", "cancelled"}
VALID_MESSAGE_STATUSES = {"pending", "streaming", "complete", "error", "cancelled"}


def list_chats(user_id: str) -> list[ChatMetadata]:
    response = (
        get_supabase_client()
        .table(CHATS_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .order("updated_at", desc=True)
        .execute()
    )

    return [_chat_metadata_from_row(row) for row in response.data or []]


def get_chat(user_id: str, chat_id: str) -> Chat | None:
    chat_row = _get_chat_row(user_id, chat_id)
    if chat_row is None:
        return None

    response = (
        get_supabase_client()
        .table(CHAT_MESSAGES_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("chat_id", chat_id)
        .order("position")
        .execute()
    )
    messages = [_message_from_row(row) for row in response.data or []]
    metadata = _chat_metadata_from_row(chat_row)
    return Chat(
        id=metadata.id,
        title=metadata.title,
        createdAt=metadata.createdAt,
        updatedAt=metadata.updatedAt,
        status=metadata.status,
        messages=messages,
    )


def save_chat(user_id: str, chat: Chat) -> Chat:
    chat_row = _chat_to_row(user_id, chat)
    client = get_supabase_client()
    _ensure_chat_is_not_owned_by_another_user(chat.id, user_id)

    client.table(CHATS_TABLE).upsert(
        chat_row,
        on_conflict="id",
    ).execute()

    client.table(CHAT_MESSAGES_TABLE).delete().eq("user_id", user_id).eq("chat_id", chat.id).execute()

    message_rows = [
        _message_to_row(user_id, chat.id, position, message)
        for position, message in enumerate(chat.messages)
    ]
    if message_rows:
        client.table(CHAT_MESSAGES_TABLE).insert(message_rows).execute()

    persisted = get_chat(user_id, chat.id)
    return persisted if persisted is not None else chat


def update_chat_status(user_id: str, chat_id: str, status: ChatStatus | None) -> None:
    payload: dict[str, Any] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if status in VALID_CHAT_STATUSES:
        payload["status"] = status

    get_supabase_client().table(CHATS_TABLE).update(payload).eq("user_id", user_id).eq("id", chat_id).execute()


def apply_run_event(user_id: str, chat_id: str, run_id: str, event: LLMEvent | dict[str, Any]) -> Chat | None:
    chat = get_chat(user_id, chat_id)
    if chat is None:
        return None

    parsed_event = LLM_EVENT_ADAPTER.validate_python(event)
    messages = _apply_assistant_event(chat.messages, run_id, parsed_event)
    return save_chat(
        user_id,
        chat.model_copy(
            update={
                "updatedAt": parsed_event.createdAt,
                "status": _chat_status_for_event(parsed_event),
                "messages": messages,
            }
        ),
    )


def _get_chat_row(user_id: str, chat_id: str) -> dict[str, Any] | None:
    response = (
        get_supabase_client()
        .table(CHATS_TABLE)
        .select("*")
        .eq("user_id", user_id)
        .eq("id", chat_id)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None


def _ensure_chat_is_not_owned_by_another_user(chat_id: str, user_id: str) -> None:
    response = (
        get_supabase_client()
        .table(CHATS_TABLE)
        .select("id,user_id")
        .eq("id", chat_id)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    if rows and rows[0].get("user_id") != user_id:
        raise PermissionError("Chat belongs to another user.")


def _chat_metadata_from_row(row: dict[str, Any]) -> ChatMetadata:
    return ChatMetadata(
        id=str(row["id"]),
        title=row.get("title") if isinstance(row.get("title"), str) else None,
        createdAt=_datetime_to_unix_ms(row.get("created_at")),
        updatedAt=_datetime_to_unix_ms(row.get("updated_at")),
        status=_normalize_chat_status(row.get("status")),
    )


def _message_from_row(row: dict[str, Any]) -> ChatMessage:
    base = {
        "id": str(row["id"]),
        "createdAt": _datetime_to_unix_ms(row.get("created_at")),
        "updatedAt": _datetime_to_unix_ms(row.get("updated_at")),
        "status": _normalize_message_status(row.get("status")),
        "role": row.get("role"),
    }

    if row.get("role") == "user":
        return UserMessage(
            **base,
            content=row.get("content") if isinstance(row.get("content"), str) else "",
            attachments=_attachments_from_value(row.get("attachments")),
        )

    return AssistantMessage(
        **{**base, "role": "assistant"},
        runId=row.get("run_id") if isinstance(row.get("run_id"), str) else None,
        events=_events_from_value(row.get("events")),
    )


def _chat_to_row(user_id: str, chat: Chat) -> dict[str, Any]:
    created_at = _min_timestamp([chat.createdAt, *[message.createdAt for message in chat.messages]])
    updated_at = _max_timestamp([chat.updatedAt, *[message.updatedAt for message in chat.messages]])
    return {
        "id": chat.id,
        "user_id": user_id,
        "title": chat.title or _get_chat_title(chat.messages),
        "status": chat.status or "idle",
        "created_at": _unix_ms_to_iso(created_at),
        "updated_at": _unix_ms_to_iso(updated_at),
    }


def _message_to_row(user_id: str, chat_id: str, position: int, message: ChatMessage) -> dict[str, Any]:
    parsed_message = CHAT_MESSAGE_ADAPTER.validate_python(message)
    row: dict[str, Any] = {
        "id": parsed_message.id,
        "chat_id": chat_id,
        "user_id": user_id,
        "position": position,
        "role": parsed_message.role,
        "status": parsed_message.status,
        "created_at": _unix_ms_to_iso(parsed_message.createdAt),
        "updated_at": _unix_ms_to_iso(parsed_message.updatedAt),
        "content": "",
        "attachments": [],
        "run_id": None,
        "events": [],
    }

    if isinstance(parsed_message, UserMessage):
        row["content"] = parsed_message.content
        row["attachments"] = [attachment.model_dump(mode="json") for attachment in parsed_message.attachments]
        return row

    row["run_id"] = parsed_message.runId
    row["events"] = [event.model_dump(mode="json") for event in parsed_message.events]
    return row


def _attachments_from_value(value: Any) -> list[MessageAttachment]:
    if not isinstance(value, list):
        return []

    attachments: list[MessageAttachment] = []
    for item in value:
        try:
            attachments.append(MessageAttachment.model_validate(item))
        except Exception:
            continue
    return attachments


def _events_from_value(value: Any) -> list[LLMEvent]:
    if not isinstance(value, list):
        return []

    events: list[LLMEvent] = []
    for item in value:
        try:
            events.append(LLM_EVENT_ADAPTER.validate_python(item))
        except Exception:
            continue
    return events


def _apply_assistant_event(messages: list[ChatMessage], run_id: str, event: LLMEvent) -> list[ChatMessage]:
    assistant_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if isinstance(messages[index], AssistantMessage) and messages[index].runId == run_id
        ),
        -1,
    )

    if assistant_index == -1:
        return [*messages, _create_assistant_message(run_id, event)]

    next_messages = list(messages)
    assistant_message = next_messages[assistant_index]
    if not isinstance(assistant_message, AssistantMessage):
        return messages

    next_messages[assistant_index] = assistant_message.model_copy(
        update={
            "updatedAt": event.createdAt,
            "status": _assistant_status_for_event(event),
            "events": _append_assistant_events(list(assistant_message.events), event),
        }
    )
    return next_messages


def _create_assistant_message(run_id: str, event: LLMEvent) -> AssistantMessage:
    return AssistantMessage(
        id=str(uuid.uuid4()),
        createdAt=event.createdAt,
        updatedAt=event.createdAt,
        status=_assistant_status_for_event(event),
        role="assistant",
        runId=run_id,
        events=[event],
    )


def _append_assistant_events(events: list[LLMEvent], event: LLMEvent) -> list[LLMEvent]:
    if event.type == "token" and events and events[-1].type == "token":
        previous_event = events[-1]
        events[-1] = previous_event.model_copy(update={"value": previous_event.value + event.value})
        return events

    events.append(event)
    return events


def _assistant_status_for_event(event: LLMEvent) -> MessageStatus:
    if event.type == "stream_error":
        return "error"
    if event.type == "stream_cancelled":
        return "cancelled"
    if event.type == "stream_end":
        return "complete"
    return "streaming"


def _chat_status_for_event(event: LLMEvent) -> ChatStatus:
    if event.type == "stream_error":
        return "error"
    if event.type == "stream_cancelled":
        return "cancelled"
    if event.type == "stream_end":
        return "idle"
    return "streaming"


def _get_chat_title(messages: list[ChatMessage]) -> str | None:
    for message in messages:
        if isinstance(message, UserMessage) and message.content.strip():
            return message.content.strip().splitlines()[0][:80]
    return None


def _normalize_chat_status(value: Any) -> ChatStatus | None:
    return value if value in VALID_CHAT_STATUSES else None


def _normalize_message_status(value: Any) -> MessageStatus:
    return value if value in VALID_MESSAGE_STATUSES else "complete"


def _datetime_to_unix_ms(value: Any) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        normalized_value = value.replace("Z", "+00:00")
        try:
            return int(datetime.fromisoformat(normalized_value).timestamp() * 1000)
        except ValueError:
            pass
    return _now_ms()


def _unix_ms_to_iso(value: int) -> str:
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _min_timestamp(values: list[int]) -> int:
    return min(values) if values else _now_ms()


def _max_timestamp(values: list[int]) -> int:
    return max(values) if values else _now_ms()
