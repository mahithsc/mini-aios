from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from .db import DB_PATH, get_db_connection, initialize_app_db
from .workspace import get_project_root, get_workspace_dir, is_production, resolve_workspace_path
from server.types.chat import (
    AssistantMessage,
    ChatMessage,
    ChatMetadata,
    LLMEvent,
    MessageAttachment,
    UserMessage,
)

CHAT_MESSAGE_ADAPTER = TypeAdapter(ChatMessage)
LLM_EVENT_ADAPTER = TypeAdapter(LLMEvent)
VALID_CHAT_STATUSES = {"idle", "streaming", "error", "cancelled"}
SESSION_DIR = resolve_workspace_path("session")
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LEGACY_ROOT_SESSION_DIR = get_project_root() / "session"
_LEGACY_DEV_SESSION_DIR = _PROJECT_ROOT / "workspace" / "session"
_CHAT_STORAGE_READY: set[str] = set()
_CHAT_STORAGE_LOCK = threading.Lock()
log = logging.getLogger(__name__)


def _sanitize_path_segment(value: str, fallback: str) -> str:
    sanitized = "".join(
        character if character.isalnum() or character in "._-" else "-" for character in value
    )
    sanitized = sanitized.strip("._-")
    return sanitized or fallback


def _migrate_attachment_paths(chat_id: str, messages: list[Any]) -> bool:
    changed = False
    legacy_prefix = f"uploads/{_sanitize_path_segment(chat_id, 'chat')}/"
    next_prefix = f"session/{_sanitize_path_segment(chat_id, 'chat')}/uploads/"

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


def _load_raw_session_messages(session_path: Path) -> list[Any]:
    messages = json.loads(session_path.read_text(encoding="utf-8"))
    if not isinstance(messages, list):
        return []
    return messages


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
                        attachment.filePath,
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
            source_path = raw_path if raw_path.is_absolute() else source_workspace / raw_path
            source_path = source_path.resolve()
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

    # Normalize the oldest upload layout before translating paths from the
    # source workspace to the active workspace.
    _migrate_attachment_paths(chat_id, raw_messages)
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

    return (
        _remap_imported_attachment_paths(
            messages,
            source_workspace=source_workspace,
            target_workspace=target_workspace,
        ),
        skipped,
    )


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
        session_path = (
            session_dir / file_name
            if isinstance(file_name, str) and file_name
            else session_dir / _sanitize_path_segment(chat_id, "chat") / "chat.json"
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
        else get_workspace_dir().resolve()
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
    if previous is not None:
        return ChatImportReport(
            source_path=source_key,
            already_imported=True,
            chat_count=previous[0],
            message_count=previous[1],
            event_count=previous[2],
            attachment_count=previous[3],
            skipped_count=previous[4],
        )

    entries = _import_entries(source_dir) if source_dir.exists() else []
    if not entries:
        return ChatImportReport(source_path=source_key)

    prepared: list[tuple[dict[str, Any], list[ChatMessage], int]] = []
    source_workspace = source_dir.parent
    for entry, session_path in entries:
        chat_id = entry["id"]
        messages, skipped = _load_import_messages(
            chat_id,
            session_path,
            source_workspace=source_workspace,
            target_workspace=target_root,
        )
        prepared.append((entry, messages, skipped))

    chat_count = 0
    message_count = 0
    event_count = 0
    attachment_count = 0
    skipped_count = 0

    with get_db_connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for entry, messages, skipped in prepared:
            chat_id = entry["id"]
            skipped_count += skipped
            if conn.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone():
                skipped_count += 1
                continue

            fallback_timestamp = _get_manifest_timestamp_ms(entry.get("addedAt"))
            created_at = messages[0].createdAt if messages else fallback_timestamp
            updated_at = messages[-1].updatedAt if messages else fallback_timestamp
            status = entry.get("status")
            if status not in VALID_CHAT_STATUSES:
                status = "idle"
            title = entry.get("title")
            if not isinstance(title, str) or not title.strip():
                title = _get_chat_title(messages)

            conn.execute(
                """
                INSERT INTO chats (id, title, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
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

        conn.execute(
            """
            INSERT INTO chat_imports
                (source_path, imported_at, chat_count, message_count, event_count,
                 attachment_count, skipped_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
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


def initialize_chat_storage() -> list[ChatImportReport]:
    db_key = str(Path(DB_PATH).expanduser().resolve())
    if db_key in _CHAT_STORAGE_READY:
        return []

    with _CHAT_STORAGE_LOCK:
        if db_key in _CHAT_STORAGE_READY:
            return []

        initialize_app_db(DB_PATH)
        sources = [Path(SESSION_DIR)]
        if not is_production():
            sources.extend((_LEGACY_ROOT_SESSION_DIR, _LEGACY_DEV_SESSION_DIR))
        sources = list(dict.fromkeys(source.expanduser().resolve() for source in sources))

        reports = [
            migrate_legacy_chat_sessions(
                source,
                db_path=DB_PATH,
                target_workspace=get_workspace_dir(),
            )
            for source in sources
        ]
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
                    attachment.filePath,
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
        conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        _insert_message_rows(conn, chat_id, normalized_messages)

    return DB_PATH
