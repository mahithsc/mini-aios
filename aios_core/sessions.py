from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from .workspace import resolve_workspace_path
from server.types.chat import AssistantMessage, ChatMessage, ChatMetadata, LLMEvent, UserMessage

CHAT_MESSAGE_ADAPTER = TypeAdapter(ChatMessage)
VALID_CHAT_STATUSES = {"idle", "streaming", "error", "cancelled"}
SESSION_DIR = resolve_workspace_path("session")
SESSION_MANIFEST_PATH = SESSION_DIR / "session_manifest.json"


def _sanitize_path_segment(value: str, fallback: str) -> str:
    sanitized = "".join(
        character if character.isalnum() or character in "._-" else "-" for character in value
    )
    sanitized = sanitized.strip("._-")
    return sanitized or fallback


def _create_manifest_timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _infer_manifest_added_at(entry: dict[str, Any]) -> str:
    file_name = entry.get("file")
    if isinstance(file_name, str):
        try:
            return datetime.strptime(file_name, "chat_%Y%m%d_%H%M%S.json").isoformat(
                timespec="seconds"
            )
        except ValueError:
            pass

    return _create_manifest_timestamp()


def load_manifest() -> list[dict[str, Any]]:
    if not SESSION_MANIFEST_PATH.exists():
        return []

    with SESSION_MANIFEST_PATH.open(encoding="utf-8") as handle:
        manifest = json.load(handle)

    if not isinstance(manifest, list):
        return []

    normalized_manifest = []
    manifest_changed = False

    for entry in manifest:
        if not isinstance(entry, dict):
            continue

        normalized_entry = dict(entry)
        added_at = normalized_entry.get("addedAt")
        if not isinstance(added_at, str) or not added_at:
            normalized_entry["addedAt"] = _infer_manifest_added_at(normalized_entry)
            manifest_changed = True

        normalized_manifest.append(normalized_entry)

    if manifest_changed:
        save_manifest(normalized_manifest)

    return normalized_manifest


def save_manifest(manifest: list[dict[str, Any]]) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    with SESSION_MANIFEST_PATH.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)


def get_sandbox_relative_dir(owner_id: str) -> Path:
    return Path("session") / _sanitize_path_segment(owner_id, "chat")


def get_sandbox_uploads_relative_dir(owner_id: str) -> Path:
    return get_sandbox_relative_dir(owner_id) / "uploads"


def get_sandbox_files_relative_dir(owner_id: str) -> Path:
    return get_sandbox_relative_dir(owner_id) / "files"


def get_sandbox_artifacts_relative_dir(owner_id: str) -> Path:
    return get_sandbox_relative_dir(owner_id) / "artifacts"


def get_sandbox_artifact_relative_dir(owner_id: str, artifact_id: str) -> Path:
    return get_sandbox_artifacts_relative_dir(owner_id) / _sanitize_path_segment(artifact_id, "artifact")


def get_sandbox_transcript_relative_path(owner_id: str) -> Path:
    return get_sandbox_relative_dir(owner_id) / "chat.json"


def get_sandbox_artifact_entrypoint_relative_path(owner_id: str, artifact_id: str) -> Path:
    return get_sandbox_artifact_relative_dir(owner_id, artifact_id) / "index.html"


def get_chat_session_relative_dir(chat_id: str) -> Path:
    return get_sandbox_relative_dir(chat_id)


def get_chat_uploads_relative_dir(chat_id: str) -> Path:
    return get_sandbox_uploads_relative_dir(chat_id)


def get_chat_files_relative_dir(chat_id: str) -> Path:
    return get_sandbox_files_relative_dir(chat_id)


def get_chat_artifacts_relative_dir(chat_id: str) -> Path:
    return get_sandbox_artifacts_relative_dir(chat_id)


def get_chat_artifact_relative_dir(chat_id: str, artifact_id: str) -> Path:
    return get_sandbox_artifact_relative_dir(chat_id, artifact_id)


def get_chat_artifact_entrypoint_relative_path(chat_id: str, artifact_id: str) -> Path:
    return get_sandbox_artifact_entrypoint_relative_path(chat_id, artifact_id)


def _sandbox_dir(owner_id: str) -> Path:
    return Path(SESSION_DIR) / _sanitize_path_segment(owner_id, "chat")


def _sandbox_transcript_file(owner_id: str) -> Path:
    return _sandbox_dir(owner_id) / "chat.json"


def _sandbox_uploads_dir(owner_id: str) -> Path:
    return _sandbox_dir(owner_id) / "uploads"


def _sandbox_files_dir(owner_id: str) -> Path:
    return _sandbox_dir(owner_id) / "files"


def _sandbox_artifacts_dir(owner_id: str) -> Path:
    return _sandbox_dir(owner_id) / "artifacts"


def get_sandbox_dir(owner_id: str) -> Path:
    return _sandbox_dir(owner_id)


def get_sandbox_transcript_path(owner_id: str) -> Path:
    return _sandbox_transcript_file(owner_id)


def get_sandbox_files_dir(owner_id: str) -> Path:
    return _sandbox_files_dir(owner_id)


def get_sandbox_artifacts_dir(owner_id: str) -> Path:
    return _sandbox_artifacts_dir(owner_id)


def get_sandbox_artifact_dir(owner_id: str, artifact_id: str) -> Path:
    return _sandbox_artifacts_dir(owner_id) / _sanitize_path_segment(artifact_id, "artifact")


def get_sandbox_artifact_entrypoint_path(owner_id: str, artifact_id: str) -> Path:
    return get_sandbox_artifact_dir(owner_id, artifact_id) / "index.html"


def get_chat_files_dir(chat_id: str) -> Path:
    return get_sandbox_files_dir(chat_id)


def get_chat_artifacts_dir(chat_id: str) -> Path:
    return get_sandbox_artifacts_dir(chat_id)


def get_chat_artifact_dir(chat_id: str, artifact_id: str) -> Path:
    return get_sandbox_artifact_dir(chat_id, artifact_id)


def get_chat_artifact_entrypoint_path(chat_id: str, artifact_id: str) -> Path:
    return get_sandbox_artifact_entrypoint_path(chat_id, artifact_id)


def _ensure_sandbox_dirs(owner_id: str) -> Path:
    session_dir = _sandbox_dir(owner_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    _sandbox_uploads_dir(owner_id).mkdir(parents=True, exist_ok=True)
    _sandbox_files_dir(owner_id).mkdir(parents=True, exist_ok=True)
    _sandbox_artifacts_dir(owner_id).mkdir(parents=True, exist_ok=True)
    return session_dir


def _ensure_chat_dirs(chat_id: str) -> Path:
    return _ensure_sandbox_dirs(chat_id)


def ensure_chat_artifact_dir(chat_id: str, artifact_id: str) -> Path:
    _ensure_chat_dirs(chat_id)
    artifact_dir = get_chat_artifact_dir(chat_id, artifact_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def _get_session_entry(chat_id: str) -> dict[str, Any] | None:
    manifest = load_manifest()
    return next((entry for entry in manifest if entry.get("id") == chat_id), None)


def _session_file_from_entry(chat_id: str, session_entry: dict[str, Any]) -> Path:
    file_name = session_entry.get("file")
    if isinstance(file_name, str) and file_name:
        return Path(SESSION_DIR) / file_name
    return _sandbox_transcript_file(chat_id)


def _update_manifest_entry(chat_id: str, updated_entry: dict[str, Any]) -> None:
    manifest = load_manifest()
    for entry in manifest:
        if entry.get("id") == chat_id:
            entry.update(updated_entry)
            save_manifest(manifest)
            return


def _migrate_attachment_paths(chat_id: str, messages: list[Any]) -> bool:
    changed = False
    legacy_prefix = f"uploads/{_sanitize_path_segment(chat_id, 'chat')}/"
    next_prefix = f"{get_sandbox_uploads_relative_dir(chat_id).as_posix()}/"

    for message in messages:
        if not isinstance(message, dict):
            continue

        attachments = message.get("attachments")
        if not isinstance(attachments, list):
            continue

        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue

            file_path = attachment.get("filePath")
            if isinstance(file_path, str) and file_path.startswith(legacy_prefix):
                attachment["filePath"] = file_path.replace(legacy_prefix, next_prefix, 1)
                changed = True

    return changed


def _migrate_legacy_upload_dir(chat_id: str) -> None:
    legacy_upload_dir = Path.cwd() / "uploads" / _sanitize_path_segment(chat_id, "chat")
    target_upload_dir = _sandbox_uploads_dir(chat_id)
    if not legacy_upload_dir.exists() or legacy_upload_dir == target_upload_dir:
        return

    target_upload_dir.parent.mkdir(parents=True, exist_ok=True)

    if not target_upload_dir.exists():
        shutil.move(str(legacy_upload_dir), str(target_upload_dir))
        return

    for source in legacy_upload_dir.iterdir():
        destination = target_upload_dir / source.name
        if destination.exists():
            continue
        shutil.move(str(source), str(destination))

    try:
        legacy_upload_dir.rmdir()
    except OSError:
        pass


def _load_raw_session_messages(session_path: Path) -> list[Any]:
    messages = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(messages, list):
        return []
    return messages


def _migrate_session_entry(chat_id: str, session_entry: dict[str, Any], *, persist_manifest: bool) -> Path:
    target_path = _sandbox_transcript_file(chat_id)
    current_path = _session_file_from_entry(chat_id, session_entry)
    _ensure_sandbox_dirs(chat_id)
    _migrate_legacy_upload_dir(chat_id)

    if current_path.exists():
        raw_messages = _load_raw_session_messages(current_path)
        attachments_changed = _migrate_attachment_paths(chat_id, raw_messages)
        should_write_target = attachments_changed or current_path != target_path or not target_path.exists()

        if should_write_target:
            target_path.write_text(json.dumps(raw_messages, indent=2), encoding="utf-8")

        if current_path != target_path and current_path.exists():
            current_path.unlink(missing_ok=True)

    relative_target_path = str(target_path.relative_to(Path(SESSION_DIR)))
    if session_entry.get("file") != relative_target_path:
        session_entry["file"] = relative_target_path
        if persist_manifest:
            _update_manifest_entry(chat_id, session_entry)

    return target_path


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


def _get_chat_title(messages: list[ChatMessage]) -> str | None:
    for message in messages:
        if isinstance(message, UserMessage) and message.content.strip():
            return message.content.strip().splitlines()[0][:80]

    return None


def _get_manifest_timestamp_ms(value: Any) -> int:
    if not isinstance(value, str) or not value:
        return int(datetime.now().timestamp() * 1000)

    try:
        return int(datetime.fromisoformat(value).timestamp() * 1000)
    except ValueError:
        return int(datetime.now().timestamp() * 1000)


def load_chat_session(chat_id: str) -> list[ChatMessage]:
    session_entry = _get_session_entry(chat_id)

    if session_entry is None:
        return []

    session_path = _migrate_session_entry(chat_id, dict(session_entry), persist_manifest=True)
    if not session_path.exists():
        return []

    messages = _load_raw_session_messages(session_path)
    return [_normalize_chat_message(message, index=index) for index, message in enumerate(messages)]


def list_chat_history() -> list[ChatMetadata]:
    history: list[ChatMetadata] = []

    for entry in load_manifest():
        if not isinstance(entry, dict):
            continue

        chat_id = entry.get("id")
        if not isinstance(chat_id, str) or not chat_id:
            continue

        messages = load_chat_session(chat_id)
        fallback_timestamp = _get_manifest_timestamp_ms(entry.get("addedAt"))

        created_at = messages[0].createdAt if messages else fallback_timestamp
        updated_at = messages[-1].updatedAt if messages else fallback_timestamp
        status = entry.get("status")

        history.append(
            ChatMetadata(
                id=chat_id,
                title=_get_chat_title(messages) or entry.get("title"),
                createdAt=created_at,
                updatedAt=updated_at,
                status=status if status in VALID_CHAT_STATUSES else None,
            )
        )

    history.sort(key=lambda chat: chat.updatedAt, reverse=True)
    return history


def update_chat_title(chat_id: str, title: str | None) -> None:
    manifest = load_manifest()
    session_entry = next((entry for entry in manifest if entry.get("id") == chat_id), None)

    if session_entry is None:
        return

    if title:
        session_entry["title"] = title
    else:
        session_entry.pop("title", None)

    save_manifest(manifest)


def get_chat_metadata(chat_id: str) -> ChatMetadata | None:
    entry = _get_session_entry(chat_id)
    if entry is None:
        return None

    messages = load_chat_session(chat_id)
    fallback_timestamp = _get_manifest_timestamp_ms(entry.get("addedAt"))
    status = entry.get("status")

    return ChatMetadata(
        id=chat_id,
        title=_get_chat_title(messages) or entry.get("title"),
        createdAt=messages[0].createdAt if messages else fallback_timestamp,
        updatedAt=messages[-1].updatedAt if messages else fallback_timestamp,
        status=status if status in VALID_CHAT_STATUSES else None,
    )


def update_chat_status(chat_id: str, status: str | None) -> None:
    manifest = load_manifest()
    session_entry = next((entry for entry in manifest if entry.get("id") == chat_id), None)

    if session_entry is None:
        return

    if status in VALID_CHAT_STATUSES:
        session_entry["status"] = status
    else:
        session_entry.pop("status", None)

    save_manifest(manifest)

def save_chat_session(chat_id: str, messages: list[BaseModel | dict[str, Any]]) -> str:
    manifest = load_manifest()
    session_entry = next((entry for entry in manifest if entry.get("id") == chat_id), None)

    if session_entry is None:
        session_entry = {
            "id": chat_id,
            "file": f"{_sanitize_path_segment(chat_id, 'chat')}/chat.json",
            "status": "idle",
            "addedAt": _create_manifest_timestamp(),
        }
        manifest.append(session_entry)
    else:
        if session_entry.get("status") not in VALID_CHAT_STATUSES:
            session_entry["status"] = "idle"
        session_entry.setdefault("addedAt", _create_manifest_timestamp())

    _ensure_sandbox_dirs(chat_id)
    _migrate_legacy_upload_dir(chat_id)
    session_path = _sandbox_transcript_file(chat_id)
    serializable_messages = [
        _normalize_chat_message(message, index=index).model_dump(mode="json")
        for index, message in enumerate(messages)
    ]
    _migrate_attachment_paths(chat_id, serializable_messages)
    session_entry["file"] = str(session_path.relative_to(Path(SESSION_DIR)))
    session_path.write_text(json.dumps(serializable_messages, indent=2), encoding="utf-8")
    save_manifest(manifest)

    return str(session_path)
