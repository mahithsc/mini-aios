from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from server.types.chat import (
    AssistantMessage,
    ChatMessage,
    ChatMetadata,
    LLMEvent,
    MessageAttachment,
    UserMessage,
)

from .db import DB_PATH, get_db_connection, initialize_app_db
from .workspace import (
    get_data_dir,
    get_sessions_dir,
    is_production,
)

CHAT_MESSAGE_ADAPTER = TypeAdapter(ChatMessage)
LLM_EVENT_ADAPTER = TypeAdapter(LLMEvent)
VALID_CHAT_STATUSES = {"idle", "streaming", "error", "cancelled"}
SESSION_DIR = get_sessions_dir()
SESSION_MANIFEST_PATH = SESSION_DIR / "session_manifest.json"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LEGACY_ROOT_SESSION_DIR = _PROJECT_ROOT / "session"
_LEGACY_DEV_SESSION_DIR = _PROJECT_ROOT / "workspace" / "session"
_LEGACY_SQLITE_CHAT_DB = (
    Path("~/.mini-aios/state/aios.db").expanduser()
    if is_production()
    else _PROJECT_ROOT / "state" / "aios.db"
)
_CHAT_STORAGE_READY: set[str] = set()
_CHAT_STORAGE_LOCK = threading.Lock()
log = logging.getLogger(__name__)


def _legacy_sqlite_chat_db_candidates() -> list[Path]:
    candidates: list[Path] = []
    isolated_data_root = bool(os.getenv("AIOS_DATA_DIR"))
    if not isolated_data_root:
        candidates.append(_LEGACY_SQLITE_CHAT_DB)
    archived_state_dir = (
        get_data_dir() / "legacy" / "storage-layout-v1" / "state"
    )
    if archived_state_dir.is_dir():
        candidates.extend(
            path
            for path in archived_state_dir.glob("aios.db*")
            if path.name == "aios.db" or path.name.startswith("aios.db.conflict-")
        )
    if not isolated_data_root:
        configured_state = os.getenv("AIOS_STATE_DIR")
        if configured_state:
            candidates.append(Path(configured_state).expanduser() / "aios.db")
        configured_home = os.getenv("AIOS_HOME")
        if configured_home:
            candidates.append(Path(configured_home).expanduser() / "state" / "aios.db")
    return list(dict.fromkeys(path.resolve() for path in candidates))


def _legacy_chat_session_candidates() -> list[Path]:
    candidates = [Path(SESSION_DIR)]
    if not is_production() and not os.getenv("AIOS_DATA_DIR"):
        candidates.extend((_LEGACY_ROOT_SESSION_DIR, _LEGACY_DEV_SESSION_DIR))
    return list(
        dict.fromkeys(candidate.expanduser().resolve() for candidate in candidates)
    )


def _sanitize_path_segment(value: str, fallback: str) -> str:
    sanitized = "".join(
        character if character.isalnum() or character in "._-" else "-" for character in value
    )
    sanitized = sanitized.strip("._-")
    if sanitized == value and sanitized not in {".", ".."}:
        return sanitized
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized or fallback}-{digest}"


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
    """Return the data-root-relative directory for one conversation.

    ``sandbox`` is retained in the public helper name for compatibility. New
    code should prefer the equivalent ``get_chat_*`` helpers below.
    """

    return Path("sessions") / _sanitize_path_segment(owner_id, "chat")


def get_sandbox_uploads_relative_dir(owner_id: str) -> Path:
    return get_sandbox_relative_dir(owner_id) / "uploads"


def get_sandbox_files_relative_dir(owner_id: str) -> Path:
    return get_sandbox_relative_dir(owner_id) / "scratch"


def get_sandbox_transcript_relative_path(owner_id: str) -> Path:
    return get_sandbox_relative_dir(owner_id) / "chat.json"


def get_chat_session_relative_dir(chat_id: str) -> Path:
    return get_sandbox_relative_dir(chat_id)


def get_chat_uploads_relative_dir(chat_id: str) -> Path:
    return get_sandbox_uploads_relative_dir(chat_id)


def get_chat_files_relative_dir(chat_id: str) -> Path:
    return get_sandbox_files_relative_dir(chat_id)


def get_chat_scratch_relative_dir(chat_id: str) -> Path:
    return get_sandbox_files_relative_dir(chat_id)


def _sandbox_dir(owner_id: str) -> Path:
    return Path(SESSION_DIR) / _sanitize_path_segment(owner_id, "chat")


def _sandbox_transcript_file(owner_id: str) -> Path:
    return _sandbox_dir(owner_id) / "chat.json"


def _sandbox_uploads_dir(owner_id: str) -> Path:
    return _sandbox_dir(owner_id) / "uploads"


def _sandbox_files_dir(owner_id: str) -> Path:
    return _sandbox_dir(owner_id) / "scratch"


def get_sandbox_dir(owner_id: str) -> Path:
    return _sandbox_dir(owner_id)


def get_sandbox_transcript_path(owner_id: str) -> Path:
    return _sandbox_transcript_file(owner_id)


def get_sandbox_uploads_dir(owner_id: str) -> Path:
    return _sandbox_uploads_dir(owner_id)


def get_sandbox_files_dir(owner_id: str) -> Path:
    return _sandbox_files_dir(owner_id)


def get_chat_files_dir(chat_id: str) -> Path:
    return get_sandbox_files_dir(chat_id)


def get_chat_scratch_dir(chat_id: str) -> Path:
    return get_sandbox_files_dir(chat_id)


def get_chat_uploads_dir(chat_id: str) -> Path:
    return get_sandbox_uploads_dir(chat_id)


def _ensure_sandbox_dirs(owner_id: str) -> Path:
    session_dir = _sandbox_dir(owner_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    _sandbox_uploads_dir(owner_id).mkdir(parents=True, exist_ok=True)
    _sandbox_files_dir(owner_id).mkdir(parents=True, exist_ok=True)
    return session_dir


def _ensure_chat_dirs(chat_id: str) -> Path:
    return _ensure_sandbox_dirs(chat_id)


def ensure_chat_storage_dirs(chat_id: str) -> Path:
    """Create the typed filesystem roots owned by ``chat_id``.

    A chat can exist only in SQLite after an import or restore. Runtime callers
    use this public helper before exposing scratch and upload paths to tools.
    """

    return _ensure_chat_dirs(chat_id)


def _get_session_entry(chat_id: str) -> dict[str, Any] | None:
    manifest = load_manifest()
    return next((entry for entry in manifest if entry.get("id") == chat_id), None)


def _session_file_from_entry(chat_id: str, session_entry: dict[str, Any]) -> Path:
    file_name = session_entry.get("file")
    if isinstance(file_name, str) and file_name:
        relative_path = Path(file_name)
        if relative_path.parts[:1] in (("session",), ("sessions",)):
            relative_path = Path(*relative_path.parts[1:])
        return Path(SESSION_DIR) / relative_path
    return _sandbox_transcript_file(chat_id)


def _update_manifest_entry(chat_id: str, updated_entry: dict[str, Any]) -> None:
    manifest = load_manifest()
    for entry in manifest:
        if entry.get("id") == chat_id:
            entry.update(updated_entry)
            save_manifest(manifest)
            return


def _canonical_attachment_path(chat_id: str, file_path: str) -> str:
    """Normalize a stored chat-owned path to the typed data-root layout.

    Older releases stored uploads at the data root or beside session scratch
    files. Absolute paths are left untouched because they may point at
    explicitly imported external data. Removed artifact paths are quarantined.
    """

    chat_segment = _sanitize_path_segment(chat_id, "chat")
    upload_root = Path("sessions", chat_segment, "uploads")
    invalid_path = (upload_root / ".invalid-path").as_posix()
    normalized_path = file_path.replace("\\", "/")
    if normalized_path == "scratch:":
        scratch_suffix = Path(".")
    elif normalized_path.startswith("scratch:/"):
        scratch_suffix = Path(normalized_path[len("scratch:/") :])
    elif normalized_path.startswith("scratch:"):
        return invalid_path
    else:
        scratch_suffix = None
    if scratch_suffix is not None:
        if scratch_suffix.is_absolute() or ".." in scratch_suffix.parts:
            return invalid_path
        return (Path("sessions", chat_segment, "scratch") / scratch_suffix).as_posix()
    if normalized_path == "data:":
        normalized_path = "."
        data_scoped = True
    elif normalized_path.startswith("data:/"):
        normalized_path = normalized_path[len("data:/") :]
        data_scoped = True
    elif normalized_path.startswith("data:"):
        return invalid_path
    else:
        data_scoped = False

    raw_path = Path(normalized_path)
    if raw_path.is_absolute():
        return invalid_path if data_scoped else str(raw_path)

    parts = raw_path.parts
    if parts[:1] == ("workspace",):
        parts = parts[1:]
    if ".." in parts:
        return invalid_path

    if (
        len(parts) >= 3
        and parts[0] in {"session", "sessions"}
        and parts[1] == chat_segment
    ):
        category = parts[2]
        suffix = parts[3:]
        if category == "uploads" and suffix:
            return (upload_root / Path(*suffix)).as_posix()
        if category == "artifacts":
            return invalid_path
        if category in {"files", "scratch"} and suffix:
            return (Path("sessions", chat_segment, "scratch", *suffix)).as_posix()

    if (
        len(parts) >= 3
        and parts[0] == "uploads"
        and parts[1] == chat_segment
    ):
        return (upload_root / Path(*parts[2:])).as_posix()
    if len(parts) >= 2 and parts[0] == "artifacts" and parts[1] == chat_segment:
        return invalid_path
    return raw_path.as_posix()


def _migrate_attachment_paths(chat_id: str, messages: list[Any]) -> bool:
    changed = False

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
            if isinstance(file_path, str):
                canonical_path = _canonical_attachment_path(chat_id, file_path)
            else:
                canonical_path = file_path
            if canonical_path != file_path:
                attachment["filePath"] = canonical_path
                changed = True

    return changed


def _load_raw_session_messages(session_path: Path) -> list[Any]:
    messages = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(messages, list):
        return []
    return messages


def _migrate_session_entry(chat_id: str, session_entry: dict[str, Any], *, persist_manifest: bool) -> Path:
    target_path = _sandbox_transcript_file(chat_id)
    current_path = _session_file_from_entry(chat_id, session_entry)
    _ensure_sandbox_dirs(chat_id)

    if current_path.exists():
        raw_messages = _load_raw_session_messages(current_path)
        attachments_changed = _migrate_attachment_paths(chat_id, raw_messages)
        should_write_target = attachments_changed or current_path != target_path or not target_path.exists()

        if should_write_target:
            target_path.write_text(json.dumps(raw_messages, indent=2), encoding="utf-8")

        if current_path != target_path and current_path.exists():
            current_path.unlink(missing_ok=True)

    relative_target_path = get_sandbox_transcript_relative_path(chat_id).as_posix()
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


@dataclass(frozen=True)
class ChatImportReport:
    source_path: str
    already_imported: bool = False
    chat_count: int = 0
    message_count: int = 0
    event_count: int = 0
    attachment_count: int = 0
    skipped_count: int = 0


def _event_payload_json(event: LLMEvent) -> str:
    payload = event.model_dump(mode="json")
    payload.pop("id", None)
    payload.pop("type", None)
    payload.pop("createdAt", None)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)


def _insert_message_rows(
    conn,
    chat_id: str,
    messages: list[ChatMessage],
) -> tuple[int, int, int]:
    event_count = 0
    attachment_count = 0

    for message_position, message in enumerate(messages):
        content = message.content if isinstance(message, UserMessage) else None
        run_id = message.runId if isinstance(message, AssistantMessage) else None
        conn.execute(
            """
            INSERT INTO chat_messages
                (id, chat_id, position, role, content, run_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.id,
                chat_id,
                message_position,
                message.role,
                content,
                run_id,
                message.status,
                message.createdAt,
                message.updatedAt,
            ),
        )

        if isinstance(message, UserMessage):
            for attachment_position, attachment in enumerate(message.attachments):
                conn.execute(
                    """
                    INSERT INTO message_attachments
                        (id, message_id, position, kind, name, file_path, mime_type,
                         size_bytes, content_hash, uploaded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        attachment.id,
                        message.id,
                        attachment_position,
                        attachment.kind,
                        attachment.name,
                        _canonical_attachment_path(chat_id, attachment.filePath),
                        attachment.mimeType,
                        attachment.sizeBytes,
                        attachment.uploadedAt,
                    ),
                )
                attachment_count += 1
            continue

        for event_sequence, event in enumerate(message.events):
            conn.execute(
                """
                INSERT INTO assistant_events
                    (id, message_id, sequence, type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    message.id,
                    event_sequence,
                    event.type,
                    _event_payload_json(event),
                    event.createdAt,
                ),
            )
            event_count += 1

    return len(messages), event_count, attachment_count


def _remap_imported_attachment_paths(
    chat_id: str,
    messages: list[ChatMessage],
    *,
    source_workspace: Path,
    target_workspace: Path,
) -> list[ChatMessage]:
    source_workspace = source_workspace.resolve()
    target_workspace = target_workspace.resolve()
    remapped: list[ChatMessage] = []

    for message in messages:
        if not isinstance(message, UserMessage):
            remapped.append(message)
            continue

        next_attachments: list[MessageAttachment] = []
        for attachment in message.attachments:
            raw_path = Path(attachment.filePath).expanduser()
            canonical_path = _canonical_attachment_path(
                chat_id,
                attachment.filePath,
            )
            canonical_relative = Path(canonical_path)

            canonical_upload_prefix = (
                "sessions",
                _sanitize_path_segment(chat_id, "chat"),
                "uploads",
            )
            is_chat_upload = (
                canonical_relative.parts[:3] == canonical_upload_prefix
                and len(canonical_relative.parts) > 3
            )
            invalid_path = Path(*canonical_upload_prefix, ".invalid-path").as_posix()
            if canonical_path == invalid_path:
                next_attachments.append(
                    attachment.model_copy(update={"filePath": canonical_path})
                )
                continue
            if not raw_path.is_absolute() and is_chat_upload:
                source_candidates = [
                    (source_workspace / raw_path).resolve(),
                    (source_workspace.parent / raw_path).resolve(),
                ]
                if raw_path.parts[:1] == ("workspace",):
                    source_candidates.append(
                        (source_workspace / Path(*raw_path.parts[1:])).resolve()
                    )
                source_path = next(
                    (candidate for candidate in source_candidates if candidate.is_file()),
                    source_candidates[0],
                )
                target_path = (target_workspace / canonical_relative).resolve()
                if source_path.is_file() and not target_path.exists():
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_path, target_path)
                next_attachments.append(
                    attachment.model_copy(update={"filePath": canonical_path})
                )
                continue

            source_path = (
                raw_path if raw_path.is_absolute() else source_workspace / raw_path
            ).resolve()
            try:
                stored_path = source_path.relative_to(target_workspace).as_posix()
            except ValueError:
                stored_path = str(source_path)
            next_attachments.append(
                attachment.model_copy(update={"filePath": stored_path})
            )

        remapped.append(message.model_copy(update={"attachments": next_attachments}))

    return remapped


def _load_import_messages(
    chat_id: str,
    session_path: Path,
    *,
    source_workspace: Path,
    target_workspace: Path,
) -> tuple[list[ChatMessage], int]:
    if not session_path.exists():
        return [], 0

    try:
        raw_messages = _load_raw_session_messages(session_path)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not import chat transcript %s: %s", session_path, exc)
        return [], 1

    messages: list[ChatMessage] = []
    skipped = 0
    for index, raw_message in enumerate(raw_messages):
        try:
            messages.append(_normalize_chat_message(raw_message, index=index))
        except (ValidationError, TypeError, ValueError) as exc:
            skipped += 1
            log.warning(
                "Skipping invalid message %s in %s: %s",
                index,
                session_path,
                exc,
            )

    if skipped:
        # Keep the chat out of SQLite until its entire source transcript can
        # be represented. This avoids permanently committing partial history.
        return [], skipped

    try:
        remapped_messages = _remap_imported_attachment_paths(
            chat_id,
            messages,
            source_workspace=source_workspace,
            target_workspace=target_workspace,
        )
    except OSError as exc:
        log.warning("Could not copy attachments for %s: %s", session_path, exc)
        return [], 1
    return remapped_messages, 0


def _load_import_manifest(session_dir: Path) -> list[dict[str, Any]]:
    manifest_path = session_dir / "session_manifest.json"
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read legacy chat manifest %s: %s", manifest_path, exc)
        return []
    return [entry for entry in manifest if isinstance(entry, dict)] if isinstance(manifest, list) else []


def _import_entries(session_dir: Path) -> list[tuple[dict[str, Any], Path]]:
    entries: list[tuple[dict[str, Any], Path]] = []
    seen_ids: set[str] = set()

    for entry in _load_import_manifest(session_dir):
        chat_id = entry.get("id")
        if not isinstance(chat_id, str) or not chat_id or chat_id in seen_ids:
            continue
        file_name = entry.get("file")
        if isinstance(file_name, str) and file_name:
            relative_path = Path(file_name)
            if relative_path.parts[:1] in (("session",), ("sessions",)):
                relative_path = Path(*relative_path.parts[1:])
            session_path = session_dir / relative_path
        else:
            session_path = (
                session_dir / _sanitize_path_segment(chat_id, "chat") / "chat.json"
            )
        entries.append((entry, session_path))
        seen_ids.add(chat_id)

    # Recover orphaned transcripts as well. They were invisible to the old
    # manifest-based history list, but they still contain user data.
    for session_path in sorted(session_dir.glob("*/chat.json")):
        chat_id = session_path.parent.name
        if chat_id in seen_ids:
            continue
        entries.append(
            (
                {
                    "id": chat_id,
                    "status": "idle",
                    "addedAt": datetime.fromtimestamp(
                        session_path.stat().st_mtime
                    ).isoformat(timespec="seconds"),
                },
                session_path,
            )
        )
        seen_ids.add(chat_id)

    return entries


def migrate_legacy_chat_sessions(
    session_dir: str | Path,
    *,
    db_path: str = DB_PATH,
    target_workspace: str | Path | None = None,
) -> ChatImportReport:
    """Import one JSON session directory exactly once.

    The JSON files are deliberately left untouched as a rollback/read-only
    backup. A source-path marker is committed in the same transaction as the
    imported rows, so a crash cannot leave a half-imported source marked done.
    """
    initialize_app_db(db_path)
    source_dir = Path(session_dir).expanduser().resolve()
    source_key = str(source_dir)
    target_root = (
        Path(target_workspace).expanduser().resolve()
        if target_workspace is not None
        else get_data_dir().resolve()
    )

    with get_db_connection(db_path) as conn:
        previous = conn.execute(
            """
            SELECT chat_count, message_count, event_count, attachment_count, skipped_count
            FROM chat_imports
            WHERE source_path = ?
            """,
            (source_key,),
        ).fetchone()
    already_imported = previous is not None

    entries = _import_entries(source_dir) if source_dir.exists() else []
    if not entries:
        return ChatImportReport(
            source_path=source_key,
            already_imported=already_imported,
        )

    prepared: list[tuple[dict[str, Any], list[ChatMessage], int]] = []
    source_error_count = 0
    source_workspace = source_dir.parent
    for entry, session_path in entries:
        chat_id = entry["id"]
        messages, skipped = _load_import_messages(
            chat_id,
            session_path,
            source_workspace=source_workspace,
            target_workspace=target_root,
        )
        if skipped:
            source_error_count += skipped
            continue
        prepared.append((entry, messages, skipped))

    chat_count = 0
    message_count = 0
    event_count = 0
    attachment_count = 0
    skipped_count = source_error_count

    with get_db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for entry, messages, skipped in prepared:
            chat_id = entry["id"]
            skipped_count += skipped
            fallback_timestamp = _get_manifest_timestamp_ms(entry.get("addedAt"))
            created_at = messages[0].createdAt if messages else fallback_timestamp
            updated_at = messages[-1].updatedAt if messages else fallback_timestamp
            status = entry.get("status")
            if status not in VALID_CHAT_STATUSES:
                status = "idle"
            title = entry.get("title")
            if not isinstance(title, str) or not title.strip():
                title = _get_chat_title(messages)

            existing = conn.execute(
                """
                SELECT updated_at,
                       (SELECT COUNT(*) FROM chat_messages WHERE chat_id = chats.id)
                FROM chats
                WHERE id = ?
                """,
                (chat_id,),
            ).fetchone()
            if existing is not None and (
                existing[0] > updated_at
                or (existing[0] == updated_at and existing[1] >= len(messages))
            ):
                skipped_count += 1
                continue

            if existing is not None:
                # The imported transcript is newer than the local projection.
                # Drop its canonical replay ledger in the same transaction so
                # the next model turn reseeds from the replacement history.
                conn.execute(
                    "DELETE FROM conversation_threads WHERE chat_id = ?",
                    (chat_id,),
                )
                conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
            conn.execute(
                """
                INSERT INTO chats (id, title, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (chat_id, title, status, created_at, updated_at),
            )
            inserted_messages, inserted_events, inserted_attachments = _insert_message_rows(
                conn,
                chat_id,
                messages,
            )
            chat_count += 1
            message_count += inserted_messages
            event_count += inserted_events
            attachment_count += inserted_attachments

        if source_error_count:
            conn.execute("DELETE FROM chat_imports WHERE source_path = ?", (source_key,))
        else:
            conn.execute(
                """
                INSERT INTO chat_imports
                    (source_path, imported_at, chat_count, message_count, event_count,
                     attachment_count, skipped_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    imported_at = excluded.imported_at,
                    chat_count = excluded.chat_count,
                    message_count = excluded.message_count,
                    event_count = excluded.event_count,
                    attachment_count = excluded.attachment_count,
                    skipped_count = excluded.skipped_count
                """,
                (
                    source_key,
                    int(time.time() * 1000),
                    chat_count,
                    message_count,
                    event_count,
                    attachment_count,
                    skipped_count,
                ),
            )

    report = ChatImportReport(
        source_path=source_key,
        already_imported=already_imported,
        chat_count=chat_count,
        message_count=message_count,
        event_count=event_count,
        attachment_count=attachment_count,
        skipped_count=skipped_count,
    )
    log.info(
        "Imported JSON chats from %s: %s chats, %s messages, %s events, %s attachments",
        source_key,
        chat_count,
        message_count,
        event_count,
        attachment_count,
    )
    return report


def migrate_legacy_chat_database(
    source_db: str | Path,
    *,
    db_path: str = DB_PATH,
) -> ChatImportReport:
    """Import chats from a legacy or archived SQLite database exactly once.

    The canonical database now lives at ``state/aios.db`` beneath the data
    root. Copy only chats that do not already exist there, including their
    child rows. Existing destination chat IDs always win regardless of
    timestamps, and the source database remains read-only and untouched.
    """
    initialize_app_db(db_path)
    source_path = Path(source_db).expanduser().resolve()
    target_path = Path(db_path).expanduser().resolve()
    source_key = f"sqlite:{source_path}"

    if not source_path.is_file() or source_path == target_path:
        return ChatImportReport(source_path=source_key)

    with get_db_connection(db_path) as conn:
        previous = conn.execute(
            """
            SELECT chat_count, message_count, event_count, attachment_count, skipped_count
            FROM chat_imports
            WHERE source_path = ?
            """,
            (source_key,),
        ).fetchone()
        already_imported = previous is not None

        source_uri = f"{source_path.as_uri()}?mode=ro"
        conn.execute("ATTACH DATABASE ? AS legacy_chat", (source_uri,))
        try:
            source_tables = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT name
                    FROM legacy_chat.sqlite_master
                    WHERE type = 'table'
                    """
                )
            }
            required_tables = {
                "chats",
                "chat_messages",
                "message_attachments",
                "assistant_events",
            }
            if not required_tables.issubset(source_tables):
                log.warning("Legacy chat database %s has an incomplete schema", source_path)
                return ChatImportReport(source_path=source_key)

            def source_columns(table: str) -> set[str]:
                return {
                    row[1]
                    for row in conn.execute(f"PRAGMA legacy_chat.table_info({table})")
                }

            required_columns = {
                "chats": {"id", "title", "status", "created_at", "updated_at"},
                "chat_messages": {
                    "id",
                    "chat_id",
                    "position",
                    "role",
                    "content",
                    "run_id",
                    "status",
                    "created_at",
                    "updated_at",
                },
                "message_attachments": {
                    "id",
                    "message_id",
                    "position",
                    "kind",
                    "name",
                    "file_path",
                    "mime_type",
                    "size_bytes",
                    "uploaded_at",
                },
                "assistant_events": {
                    "id",
                    "message_id",
                    "sequence",
                    "type",
                    "payload_json",
                    "created_at",
                },
            }
            source_column_sets = {
                table: source_columns(table) for table in required_tables
            }
            if any(
                not columns.issubset(source_column_sets[table])
                for table, columns in required_columns.items()
            ):
                log.warning("Legacy chat database %s has incompatible columns", source_path)
                return ChatImportReport(source_path=source_key)

            representation_columns = {
                "id",
                "attachment_id",
                "position",
                "kind",
                "status",
                "text_content",
                "file_path",
                "mime_type",
                "metadata_json",
                "created_at",
                "updated_at",
            }
            has_representations = (
                "attachment_representations" in source_tables
                and representation_columns.issubset(
                    source_columns("attachment_representations")
                )
            )
            content_hash_expression = (
                "source.content_hash"
                if "content_hash" in source_column_sets["message_attachments"]
                else "NULL"
            )
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TEMP TABLE _legacy_chat_import_ids (id TEXT PRIMARY KEY)"
            )

            # Skip an entire source chat if any globally-unique child identity
            # collides. This keeps destination data authoritative without
            # accidentally attaching source events to an unrelated message.
            representation_conflict = (
                """
                AND NOT EXISTS (
                    SELECT 1
                    FROM legacy_chat.chat_messages AS sm
                    JOIN legacy_chat.message_attachments AS sa
                      ON sa.message_id = sm.id
                    JOIN legacy_chat.attachment_representations AS sr
                      ON sr.attachment_id = sa.id
                    JOIN main.attachment_representations AS dr
                      ON dr.id = sr.id
                    JOIN main.message_attachments AS da
                      ON da.id = dr.attachment_id
                    JOIN main.chat_messages AS dm ON dm.id = da.message_id
                    WHERE sm.chat_id = source_chat.id
                      AND dm.chat_id <> source_chat.id
                )
                """
                if has_representations
                else ""
            )
            conn.execute(
                f"""
                INSERT INTO _legacy_chat_import_ids (id)
                SELECT source_chat.id
                FROM legacy_chat.chats AS source_chat
                WHERE NOT EXISTS (
                    SELECT 1 FROM main.chats AS destination_chat
                    WHERE destination_chat.id = source_chat.id
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM legacy_chat.chat_messages AS sm
                    JOIN main.chat_messages AS dm
                      ON dm.id = sm.id
                      OR (sm.run_id IS NOT NULL AND dm.run_id = sm.run_id)
                    WHERE sm.chat_id = source_chat.id
                      AND dm.chat_id <> source_chat.id
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM legacy_chat.chat_messages AS sm
                    JOIN legacy_chat.message_attachments AS sa
                      ON sa.message_id = sm.id
                    JOIN main.message_attachments AS da ON da.id = sa.id
                    JOIN main.chat_messages AS dm ON dm.id = da.message_id
                    WHERE sm.chat_id = source_chat.id
                      AND dm.chat_id <> source_chat.id
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM legacy_chat.chat_messages AS sm
                    JOIN legacy_chat.assistant_events AS se
                      ON se.message_id = sm.id
                    JOIN main.assistant_events AS de ON de.id = se.id
                    JOIN main.chat_messages AS dm ON dm.id = de.message_id
                    WHERE sm.chat_id = source_chat.id
                      AND dm.chat_id <> source_chat.id
                )
                {representation_conflict}
                """
            )

            source_chat_count = conn.execute(
                "SELECT COUNT(*) FROM legacy_chat.chats"
            ).fetchone()[0]
            chat_count = conn.execute(
                "SELECT COUNT(*) FROM _legacy_chat_import_ids"
            ).fetchone()[0]
            message_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM legacy_chat.chat_messages AS message
                JOIN _legacy_chat_import_ids AS selected ON selected.id = message.chat_id
                """
            ).fetchone()[0]
            attachment_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM legacy_chat.message_attachments AS attachment
                JOIN legacy_chat.chat_messages AS message
                  ON message.id = attachment.message_id
                JOIN _legacy_chat_import_ids AS selected ON selected.id = message.chat_id
                """
            ).fetchone()[0]
            event_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM legacy_chat.assistant_events AS event
                JOIN legacy_chat.chat_messages AS message
                  ON message.id = event.message_id
                JOIN _legacy_chat_import_ids AS selected ON selected.id = message.chat_id
                """
            ).fetchone()[0]

            conn.execute(
                """
                INSERT INTO main.chats (id, title, status, created_at, updated_at)
                SELECT source.id, source.title, source.status,
                       source.created_at, source.updated_at
                FROM legacy_chat.chats AS source
                JOIN _legacy_chat_import_ids AS selected ON selected.id = source.id
                """
            )
            conn.execute(
                """
                INSERT INTO main.chat_messages
                    (id, chat_id, position, role, content, run_id, status,
                     created_at, updated_at)
                SELECT source.id, source.chat_id, source.position, source.role,
                       source.content, source.run_id, source.status,
                       source.created_at, source.updated_at
                FROM legacy_chat.chat_messages AS source
                JOIN _legacy_chat_import_ids AS selected
                  ON selected.id = source.chat_id
                """
            )
            conn.execute(
                f"""
                INSERT INTO main.message_attachments
                    (id, message_id, position, kind, name, file_path, mime_type,
                     size_bytes, content_hash, uploaded_at)
                SELECT source.id, source.message_id, source.position, source.kind,
                       source.name, source.file_path,
                       source.mime_type, source.size_bytes, {content_hash_expression},
                       source.uploaded_at
                FROM legacy_chat.message_attachments AS source
                JOIN legacy_chat.chat_messages AS message
                  ON message.id = source.message_id
                JOIN _legacy_chat_import_ids AS selected
                  ON selected.id = message.chat_id
                """
            )
            if has_representations:
                conn.execute(
                    """
                    INSERT INTO main.attachment_representations
                        (id, attachment_id, position, kind, status, text_content,
                         file_path, mime_type, metadata_json, created_at, updated_at)
                    SELECT source.id, source.attachment_id, source.position,
                           source.kind, source.status, source.text_content,
                           source.file_path,
                           source.mime_type, source.metadata_json,
                           source.created_at, source.updated_at
                    FROM legacy_chat.attachment_representations AS source
                    JOIN legacy_chat.message_attachments AS attachment
                      ON attachment.id = source.attachment_id
                    JOIN legacy_chat.chat_messages AS message
                      ON message.id = attachment.message_id
                    JOIN _legacy_chat_import_ids AS selected
                      ON selected.id = message.chat_id
                    """
                )
            conn.execute(
                """
                INSERT INTO main.assistant_events
                    (id, message_id, sequence, type, payload_json, created_at)
                SELECT source.id, source.message_id, source.sequence,
                       source.type, source.payload_json, source.created_at
                FROM legacy_chat.assistant_events AS source
                JOIN legacy_chat.chat_messages AS message
                  ON message.id = source.message_id
                JOIN _legacy_chat_import_ids AS selected
                  ON selected.id = message.chat_id
                """
            )

            skipped_count = source_chat_count - chat_count
            conn.execute(
                """
                INSERT INTO main.chat_imports
                    (source_path, imported_at, chat_count, message_count,
                     event_count, attachment_count, skipped_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    imported_at = excluded.imported_at,
                    chat_count = excluded.chat_count,
                    message_count = excluded.message_count,
                    event_count = excluded.event_count,
                    attachment_count = excluded.attachment_count,
                    skipped_count = excluded.skipped_count
                """,
                (
                    source_key,
                    int(time.time() * 1000),
                    chat_count,
                    message_count,
                    event_count,
                    attachment_count,
                    skipped_count,
                ),
            )
            conn.execute("DROP TABLE _legacy_chat_import_ids")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            try:
                conn.execute("DROP TABLE IF EXISTS _legacy_chat_import_ids")
                conn.execute("DETACH DATABASE legacy_chat")
            except sqlite3.OperationalError:
                log.warning("Could not detach legacy chat database %s", source_path)

    _canonicalize_stored_attachment_paths(db_path)
    report = ChatImportReport(
        source_path=source_key,
        already_imported=already_imported,
        chat_count=chat_count,
        message_count=message_count,
        event_count=event_count,
        attachment_count=attachment_count,
        skipped_count=skipped_count,
    )
    log.info(
        "Imported SQLite chats from %s: %s chats, %s messages, %s events, %s attachments",
        source_path,
        chat_count,
        message_count,
        event_count,
        attachment_count,
    )
    return report


def _canonicalize_stored_attachment_paths(db_path: str = DB_PATH) -> int:
    """Rewrite legacy relative attachment paths without touching external paths."""

    updates: list[tuple[str, str]] = []
    representation_updates: list[tuple[str, str]] = []
    with get_db_connection(db_path) as conn:
        for attachment_id, chat_id, file_path in conn.execute(
            """
            SELECT attachment.id, message.chat_id, attachment.file_path
            FROM message_attachments AS attachment
            JOIN chat_messages AS message ON message.id = attachment.message_id
            """
        ):
            if not isinstance(file_path, str):
                continue
            canonical_path = _canonical_attachment_path(chat_id, file_path)
            if canonical_path != file_path:
                updates.append((canonical_path, attachment_id))

        for representation_id, chat_id, file_path in conn.execute(
            """
            SELECT representation.id, message.chat_id, representation.file_path
            FROM attachment_representations AS representation
            JOIN message_attachments AS attachment
              ON attachment.id = representation.attachment_id
            JOIN chat_messages AS message ON message.id = attachment.message_id
            WHERE representation.file_path IS NOT NULL
            """
        ):
            if not isinstance(file_path, str):
                continue
            canonical_path = _canonical_attachment_path(chat_id, file_path)
            if canonical_path != file_path:
                representation_updates.append((canonical_path, representation_id))

        if updates:
            conn.executemany(
                "UPDATE message_attachments SET file_path = ? WHERE id = ?",
                updates,
            )
        if representation_updates:
            conn.executemany(
                "UPDATE attachment_representations SET file_path = ? WHERE id = ?",
                representation_updates,
            )
    return len(updates) + len(representation_updates)


def initialize_chat_storage() -> list[ChatImportReport]:
    db_key = str(Path(DB_PATH).expanduser().resolve())
    if db_key in _CHAT_STORAGE_READY:
        return []

    with _CHAT_STORAGE_LOCK:
        if db_key in _CHAT_STORAGE_READY:
            return []

        initialize_app_db(DB_PATH)
        sources = _legacy_chat_session_candidates()

        reports: list[ChatImportReport] = []
        target_db = Path(DB_PATH).expanduser().resolve()
        for legacy_db in _legacy_sqlite_chat_db_candidates():
            if legacy_db.is_file() and legacy_db != target_db:
                reports.append(
                    migrate_legacy_chat_database(
                        legacy_db,
                        db_path=DB_PATH,
                    )
                )
        reports.extend(
            migrate_legacy_chat_sessions(
                source,
                db_path=DB_PATH,
                target_workspace=get_data_dir(),
            )
            for source in sources
        )
        _canonicalize_stored_attachment_paths(DB_PATH)
        _CHAT_STORAGE_READY.add(db_key)
        return reports


def _load_chat_session_from_db(chat_id: str) -> list[ChatMessage]:
    attachments_by_message: dict[str, list[dict[str, Any]]] = defaultdict(list)
    events_by_message: dict[str, list[LLMEvent]] = defaultdict(list)

    with get_db_connection(DB_PATH) as conn:
        message_rows = conn.execute(
            """
            SELECT id, role, content, run_id, status, created_at, updated_at
            FROM chat_messages
            WHERE chat_id = ?
            ORDER BY position ASC
            """,
            (chat_id,),
        ).fetchall()
        attachment_rows = conn.execute(
            """
            SELECT a.id, a.message_id, a.kind, a.name, a.file_path, a.mime_type,
                   a.size_bytes, a.uploaded_at
            FROM message_attachments AS a
            JOIN chat_messages AS m ON m.id = a.message_id
            WHERE m.chat_id = ?
            ORDER BY m.position ASC, a.position ASC
            """,
            (chat_id,),
        ).fetchall()
        event_rows = conn.execute(
            """
            SELECT e.id, e.message_id, e.type, e.payload_json, e.created_at
            FROM assistant_events AS e
            JOIN chat_messages AS m ON m.id = e.message_id
            WHERE m.chat_id = ?
            ORDER BY m.position ASC, e.sequence ASC
            """,
            (chat_id,),
        ).fetchall()

    for row in attachment_rows:
        attachment_id, message_id, kind, name, file_path, mime_type, size_bytes, uploaded_at = row
        attachments_by_message[message_id].append(
            {
                "id": attachment_id,
                "kind": kind,
                "name": name,
                "filePath": file_path,
                "mimeType": mime_type,
                "sizeBytes": size_bytes,
                "uploadedAt": uploaded_at,
            }
        )

    for event_id, message_id, event_type, payload_json, created_at in event_rows:
        try:
            payload = json.loads(payload_json)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {"value": payload}
        events_by_message[message_id].append(
            LLM_EVENT_ADAPTER.validate_python(
                {
                    **payload,
                    "id": event_id,
                    "type": event_type,
                    "createdAt": created_at,
                }
            )
        )

    messages: list[ChatMessage] = []
    for message_id, role, content, run_id, status, created_at, updated_at in message_rows:
        if role == "user":
            messages.append(
                UserMessage(
                    id=message_id,
                    createdAt=created_at,
                    updatedAt=updated_at,
                    status=status,
                    content=content or "",
                    attachments=attachments_by_message[message_id],
                )
            )
        else:
            messages.append(
                AssistantMessage(
                    id=message_id,
                    createdAt=created_at,
                    updatedAt=updated_at,
                    status=status,
                    runId=run_id,
                    events=events_by_message[message_id],
                )
            )
    return messages


def load_chat_session(chat_id: str) -> list[ChatMessage]:
    initialize_chat_storage()
    ensure_chat_storage_dirs(chat_id)
    return _load_chat_session_from_db(chat_id)


def list_chat_history() -> list[ChatMetadata]:
    initialize_chat_storage()
    with get_db_connection(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, title, created_at, updated_at, status
            FROM chats
            ORDER BY updated_at DESC, id ASC
            """
        ).fetchall()
    return [
        ChatMetadata(
            id=chat_id,
            title=title,
            createdAt=created_at,
            updatedAt=updated_at,
            status=status,
        )
        for chat_id, title, created_at, updated_at, status in rows
    ]


def create_chat(chat_id: str, title: str | None = None) -> ChatMetadata:
    initialize_chat_storage()
    now = int(time.time() * 1000)
    _ensure_sandbox_dirs(chat_id)
    with get_db_connection(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO chats (id, title, status, created_at, updated_at)
            VALUES (?, ?, 'idle', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = CASE
                    WHEN excluded.title IS NOT NULL THEN excluded.title
                    ELSE chats.title
                END
            """,
            (chat_id, title, now, now),
        )
    metadata = get_chat_metadata(chat_id)
    if metadata is None:
        raise RuntimeError(f"Failed to create chat {chat_id}")
    return metadata


def update_chat_title(chat_id: str, title: str | None) -> None:
    initialize_chat_storage()
    with get_db_connection(DB_PATH) as conn:
        conn.execute("UPDATE chats SET title = ? WHERE id = ?", (title or None, chat_id))


def get_chat_metadata(chat_id: str) -> ChatMetadata | None:
    initialize_chat_storage()
    with get_db_connection(DB_PATH) as conn:
        row = conn.execute(
            """
            SELECT id, title, created_at, updated_at, status
            FROM chats
            WHERE id = ?
            """,
            (chat_id,),
        ).fetchone()
    if row is None:
        return None
    return ChatMetadata(
        id=row[0],
        title=row[1],
        createdAt=row[2],
        updatedAt=row[3],
        status=row[4],
    )


def update_chat_status(chat_id: str, status: str | None) -> None:
    initialize_chat_storage()
    next_status = status if status in VALID_CHAT_STATUSES else "idle"
    with get_db_connection(DB_PATH) as conn:
        conn.execute("UPDATE chats SET status = ? WHERE id = ?", (next_status, chat_id))


def append_user_message(
    chat_id: str,
    message: UserMessage | dict[str, Any],
    *,
    chat_status: str | None = None,
) -> bool:
    initialize_chat_storage()
    ensure_chat_storage_dirs(chat_id)
    parsed = _normalize_chat_message(message)
    if not isinstance(parsed, UserMessage):
        raise TypeError("append_user_message requires a user message")
    next_chat_status = chat_status if chat_status in VALID_CHAT_STATUSES else None
    title = parsed.content.strip().splitlines()[0][:80] if parsed.content.strip() else None

    with get_db_connection(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM chat_messages WHERE id = ?",
            (parsed.id,),
        ).fetchone():
            return False

        conn.execute(
            """
            INSERT INTO chats (id, title, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                chat_id,
                title,
                next_chat_status or "idle",
                parsed.createdAt,
                parsed.updatedAt,
            ),
        )
        position = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM chat_messages WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO chat_messages
                (id, chat_id, position, role, content, run_id, status, created_at, updated_at)
            VALUES (?, ?, ?, 'user', ?, NULL, ?, ?, ?)
            """,
            (
                parsed.id,
                chat_id,
                position,
                parsed.content,
                parsed.status,
                parsed.createdAt,
                parsed.updatedAt,
            ),
        )
        for attachment_position, attachment in enumerate(parsed.attachments):
            conn.execute(
                """
                INSERT INTO message_attachments
                    (id, message_id, position, kind, name, file_path, mime_type,
                     size_bytes, content_hash, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    attachment.id,
                    parsed.id,
                    attachment_position,
                    attachment.kind,
                    attachment.name,
                    _canonical_attachment_path(chat_id, attachment.filePath),
                    attachment.mimeType,
                    attachment.sizeBytes,
                    attachment.uploadedAt,
                ),
            )
        conn.execute(
            """
            UPDATE chats
            SET title = COALESCE(title, ?),
                status = COALESCE(?, status),
                updated_at = CASE WHEN updated_at < ? THEN ? ELSE updated_at END
            WHERE id = ?
            """,
            (
                title,
                next_chat_status,
                parsed.updatedAt,
                parsed.updatedAt,
                chat_id,
            ),
        )
    return True


def _assistant_status_for_event(event: LLMEvent) -> str:
    if event.type == "stream_error":
        return "error"
    if event.type == "stream_cancelled":
        return "cancelled"
    if event.type == "stream_end":
        return "complete"
    return "streaming"


def _chat_status_for_event(event: LLMEvent) -> str:
    if event.type == "stream_error":
        return "error"
    if event.type == "stream_cancelled":
        return "cancelled"
    if event.type == "stream_end":
        return "idle"
    return "streaming"


def append_assistant_event(
    chat_id: str,
    run_id: str,
    event: LLMEvent | dict[str, Any],
) -> bool:
    """Persist one normalized model event without loading/replacing the chat."""
    initialize_chat_storage()
    parsed_event = LLM_EVENT_ADAPTER.validate_python(event)

    with get_db_connection(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM assistant_events WHERE id = ?",
            (parsed_event.id,),
        ).fetchone():
            return False

        conn.execute(
            """
            INSERT INTO chats (id, title, status, created_at, updated_at)
            VALUES (?, NULL, 'streaming', ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (chat_id, parsed_event.createdAt, parsed_event.createdAt),
        )
        message_row = conn.execute(
            "SELECT id, chat_id FROM chat_messages WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if message_row is None:
            message_id = str(uuid.uuid4())
            position = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM chat_messages WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO chat_messages
                    (id, chat_id, position, role, content, run_id, status,
                     created_at, updated_at)
                VALUES (?, ?, ?, 'assistant', NULL, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    chat_id,
                    position,
                    run_id,
                    _assistant_status_for_event(parsed_event),
                    parsed_event.createdAt,
                    parsed_event.createdAt,
                ),
            )
        else:
            message_id = message_row[0]
            if message_row[1] != chat_id:
                raise ValueError(
                    f"Run {run_id} belongs to chat {message_row[1]}, not {chat_id}"
                )

        sequence = conn.execute(
            """
            SELECT COALESCE(MAX(sequence), -1) + 1
            FROM assistant_events
            WHERE message_id = ?
            """,
            (message_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO assistant_events
                (id, message_id, sequence, type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                parsed_event.id,
                message_id,
                sequence,
                parsed_event.type,
                _event_payload_json(parsed_event),
                parsed_event.createdAt,
            ),
        )
        conn.execute(
            """
            UPDATE chat_messages
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                _assistant_status_for_event(parsed_event),
                parsed_event.createdAt,
                message_id,
            ),
        )
        conn.execute(
            """
            UPDATE chats
            SET status = ?,
                updated_at = CASE WHEN updated_at < ? THEN ? ELSE updated_at END
            WHERE id = ?
            """,
            (
                _chat_status_for_event(parsed_event),
                parsed_event.createdAt,
                parsed_event.createdAt,
                chat_id,
            ),
        )
    return True


def save_chat_session(chat_id: str, messages: list[BaseModel | dict[str, Any]]) -> str:
    """Compatibility whole-transcript save.

    HTTP/gateway streaming uses the incremental append helpers above. This
    replacement path remains for the CLI and the legacy endpoint that submits
    a fully hydrated desktop transcript.
    """
    initialize_chat_storage()
    serializable_messages = [
        message.model_dump(mode="json") if isinstance(message, BaseModel) else dict(message)
        for message in messages
    ]
    _migrate_attachment_paths(chat_id, serializable_messages)
    normalized_messages = [
        _normalize_chat_message(message, index=index)
        for index, message in enumerate(serializable_messages)
    ]
    _ensure_sandbox_dirs(chat_id)
    now = int(time.time() * 1000)

    with get_db_connection(DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT title, status, created_at, updated_at FROM chats WHERE id = ?",
            (chat_id,),
        ).fetchone()
        existing_title = existing[0] if existing is not None else None
        status = existing[1] if existing is not None else "idle"
        created_at = (
            normalized_messages[0].createdAt
            if normalized_messages
            else (existing[2] if existing is not None else now)
        )
        updated_at = (
            normalized_messages[-1].updatedAt
            if normalized_messages
            else (existing[3] if existing is not None else now)
        )
        title = existing_title or _get_chat_title(normalized_messages)

        conn.execute(
            """
            INSERT INTO chats (id, title, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                status = excluded.status,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at
            """,
            (chat_id, title, status, created_at, updated_at),
        )
        # This API replaces the whole desktop transcript. Canonical provider
        # history must be invalidated atomically instead of replaying stale
        # tool calls or assistant items after the replacement.
        conn.execute("DELETE FROM conversation_threads WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        _insert_message_rows(conn, chat_id, normalized_messages)

    return DB_PATH
