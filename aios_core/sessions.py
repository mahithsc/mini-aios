from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from .initialize import SESSION_DIR, load_manifest, save_manifest
from server.types.chat import AssistantMessage, ChatMessage, LLMEvent, UserMessage

CHAT_MESSAGE_ADAPTER = TypeAdapter(ChatMessage)


def _create_session_filename() -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"chat_{timestamp}.json"


def _get_session_entry(chat_id: str) -> dict[str, Any] | None:
    manifest = load_manifest()
    return next((entry for entry in manifest if entry.get("id") == chat_id), None)


def _get_legacy_message_timestamp(index: int) -> int:
    return int(datetime.now().timestamp() * 1000) + index


def _merge_assistant_events(events: list[LLMEvent]) -> list[LLMEvent]:
    merged_events: list[LLMEvent] = []

    for event in events:
        if event.type == "token" and merged_events and merged_events[-1].type == "token":
            previous_event = merged_events[-1]
            merged_events[-1] = previous_event.model_copy(
                update={"value": previous_event.value + event.value}
            )
            continue

        merged_events.append(event)

    return merged_events


def _parse_chat_message(message: BaseModel | dict[str, Any], index: int = 0) -> ChatMessage:
    payload = message.model_dump(mode="json") if isinstance(message, BaseModel) else message

    try:
        return CHAT_MESSAGE_ADAPTER.validate_python(payload)
    except ValidationError:
        if not isinstance(payload, dict):
            raise

        role = payload.get("role")
        content = payload.get("content")
        timestamp = _get_legacy_message_timestamp(index)
        base_message = {
            "id": payload.get("id", str(uuid.uuid4())),
            "createdAt": payload.get("createdAt", timestamp),
            "updatedAt": payload.get("updatedAt", timestamp),
            "status": payload.get("status", "complete"),
            "role": role,
        }

        if role == "user" and isinstance(content, str):
            return UserMessage(
                **base_message,
                content=content,
                attachments=payload.get("attachments", []),
            )

        if role == "assistant" and isinstance(content, str):
            return AssistantMessage(
                **base_message,
                events=[
                    {
                        "id": str(uuid.uuid4()),
                        "createdAt": base_message["updatedAt"],
                        "type": "token",
                        "value": content,
                    }
                ],
            )

        raise


def _normalize_chat_message(message: BaseModel | dict[str, Any], index: int = 0) -> ChatMessage:
    parsed_message = _parse_chat_message(message, index=index)

    if isinstance(parsed_message, AssistantMessage):
        merged_events = _merge_assistant_events(parsed_message.events)
        updated_at = merged_events[-1].createdAt if merged_events else parsed_message.updatedAt

        return parsed_message.model_copy(update={"events": merged_events, "updatedAt": updated_at})

    return parsed_message


def load_chat_session(chat_id: str) -> list[ChatMessage]:
    session_entry = _get_session_entry(chat_id)

    if session_entry is None:
        return []

    session_path = Path(SESSION_DIR) / session_entry["file"]
    if not session_path.exists():
        return []

    messages = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(messages, list):
        return []

    return [_normalize_chat_message(message, index=index) for index, message in enumerate(messages)]


def save_chat_session(chat_id: str, messages: list[BaseModel | dict[str, Any]]) -> str:
    manifest = load_manifest()
    session_entry = next((entry for entry in manifest if entry.get("id") == chat_id), None)

    if session_entry is None:
        session_entry = {
            "id": chat_id,
            "file": _create_session_filename(),
            "status": "new",
        }
        manifest.append(session_entry)
    else:
        session_entry["status"] = "new"

    session_path = Path(SESSION_DIR) / session_entry["file"]
    serializable_messages = [
        _normalize_chat_message(message, index=index).model_dump(mode="json")
        for index, message in enumerate(messages)
    ]
    session_path.write_text(json.dumps(serializable_messages, indent=2), encoding="utf-8")
    save_manifest(manifest)

    return str(session_path)
