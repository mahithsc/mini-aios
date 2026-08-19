"""Canonical agent conversation persistence.

The desktop transcript is a presentation model.  This module owns the ordered,
lossless items used for provider replay and the raw streaming event ledger used
for diagnostics/UI projections.  It deliberately stores provider items as JSON
without narrowing their schema so new Responses item types survive upgrades.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .db import DB_PATH, get_db_connection, initialize_app_db

MAIN_SCOPE = "main"
OPENAI_RAIL = "openai_responses"
DEFAULT_OPENAI_HISTORY_TEXT_CHARS = 120_000
DEFAULT_OPENAI_HISTORY_MEDIA_CHARS = 48 * 1024 * 1024

# Provider conversation items are persisted as schema-preserving JSON objects.
# The repository intentionally does not depend on an SDK-owned item union.
ConversationItem = dict[str, Any]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_unset=True)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json", exclude_unset=True)
        except TypeError:
            return model_dump()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_text(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _item_dict(item: Any) -> dict[str, Any]:
    value = _jsonable(item)
    if not isinstance(value, dict):
        raise TypeError(f"Conversation item must serialize to an object, got {type(value)!r}")
    return value


def _item_type(item: Mapping[str, Any]) -> str:
    explicit = item.get("type")
    if isinstance(explicit, str) and explicit:
        return explicit
    role = item.get("role")
    if isinstance(role, str) and role:
        return "message"
    return "unknown"


def _call_id(item: Mapping[str, Any]) -> str | None:
    for key in ("call_id", "tool_call_id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _tool_name(item: Mapping[str, Any]) -> str | None:
    value = item.get("name")
    return value if isinstance(value, str) and value else None


def _content_text(item: Mapping[str, Any]) -> str | None:
    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            values.append(value)
            return
        if isinstance(value, Mapping):
            for key in ("text", "refusal"):
                text = value.get(key)
                if isinstance(text, str):
                    values.append(text)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for entry in value:
                collect(entry)

    collect(item.get("content"))
    if not values and _item_type(item).endswith("_output"):
        collect(item.get("output"))
    return "".join(values) if values else None


def _provider_item_id(item: Mapping[str, Any]) -> str | None:
    value = item.get("id")
    return value if isinstance(value, str) and value else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _event_projection(
    payload: Mapping[str, Any],
) -> tuple[str | None, int | None, int | None, int | None]:
    """Extract searchable OpenAI stream coordinates without narrowing JSON."""
    candidate = payload.get("data")
    if not isinstance(candidate, Mapping):
        raw_item = payload.get("raw_item")
        candidate = raw_item if isinstance(raw_item, Mapping) else payload

    provider_item_id = candidate.get("item_id")
    if not isinstance(provider_item_id, str) or not provider_item_id:
        provider_item_id = candidate.get("id")
    if not isinstance(provider_item_id, str) or not provider_item_id:
        nested_item = candidate.get("item")
        if isinstance(nested_item, Mapping):
            nested_id = nested_item.get("id")
            provider_item_id = (
                nested_id if isinstance(nested_id, str) and nested_id else None
            )
        else:
            provider_item_id = None

    return (
        provider_item_id,
        _integer(candidate.get("output_index")),
        _integer(candidate.get("content_index")),
        _integer(candidate.get("sequence_number")),
    )


def _inline_media_chars(value: Any) -> int:
    if isinstance(value, Mapping):
        total = 0
        for key, item in value.items():
            if key in {"image_url", "file_data"} and isinstance(item, str):
                total += len(item)
            else:
                total += _inline_media_chars(item)
        return total
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return sum(_inline_media_chars(item) for item in value)
    return 0


def _item_budget(item: Mapping[str, Any]) -> tuple[int, int]:
    media_chars = _inline_media_chars(item)
    return max(0, len(_json_text(item)) - media_chars), media_chars


def _openai_atomic_groups(
    items: list[ConversationItem],
) -> list[list[ConversationItem]]:
    """Group Responses items so context trimming never orphans a tool call.

    Reasoning stays with the output it conditions, and every function call
    stays with its matching output(s). Plain user/assistant messages remain
    independently trimmable where the protocol permits it.
    """
    groups: list[list[ConversationItem]] = []
    pending: list[ConversationItem] = []

    def flush() -> None:
        if pending:
            groups.append(list(pending))
            pending.clear()

    for raw_item in items:
        item = _item_dict(raw_item)
        kind = _item_type(item)
        role = item.get("role")
        if role == "user":
            flush()
            groups.append([raw_item])
            continue
        if kind == "reasoning":
            flush()
            pending.append(raw_item)
            continue
        if kind in {"function_call", "function_call_output"}:
            pending.append(raw_item)
            continue
        if role == "assistant" or kind == "message":
            if pending:
                pending_calls = {
                    _call_id(_item_dict(entry))
                    for entry in pending
                    if _item_type(_item_dict(entry)) == "function_call"
                }
                pending_outputs = {
                    _call_id(_item_dict(entry))
                    for entry in pending
                    if _item_type(_item_dict(entry)) == "function_call_output"
                }
                pending_calls.discard(None)
                pending_outputs.discard(None)
                if pending_calls and pending_calls <= pending_outputs:
                    flush()
                    groups.append([raw_item])
                else:
                    pending.append(raw_item)
                    flush()
            else:
                groups.append([raw_item])
            continue
        if pending:
            pending.append(raw_item)
        else:
            groups.append([raw_item])
    flush()
    return groups


def _stable_item_key(
    item: Mapping[str, Any],
    *,
    run_id: str | None,
    source_message_id: str | None,
    fallback_suffix: str,
) -> str:
    kind = _item_type(item)
    if source_message_id:
        # One persisted UI message maps to one provider input item. The key is
        # independent of Session batch numbering so preflight persistence,
        # SDK writes, process retries, and recreated Session objects converge.
        return f"ui:{source_message_id}"
    provider_id = _provider_item_id(item)
    if provider_id:
        return f"id:{kind}:{provider_id}"
    call_id = _call_id(item)
    if call_id:
        return f"call:{kind}:{call_id}"
    for key in ("request_id", "approval_request_id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return f"request:{kind}:{value}"
    digest = hashlib.sha256(_json_text(item).encode("utf-8")).hexdigest()[:24]
    return f"run:{run_id or 'legacy'}:{kind}:{digest}:{fallback_suffix}"


@dataclass(frozen=True)
class StoredConversationItem:
    id: int
    position: int
    dedupe_key: str


def create_turn_row(
    conn: sqlite3.Connection,
    *,
    chat_id: str,
    turn_id: str,
    user_message_id: str,
    run_id: str,
    now: int,
) -> None:
    """Create the canonical queued turn inside an existing transaction.

    Run submission uses this helper so the durable run row and its canonical
    conversation turn become visible in the same commit. AgentRuntime calls
    :meth:`ConversationStore.create_turn` again when execution begins; the
    identity checks below deliberately make that second call idempotent.
    """

    existing = conn.execute(
        """
        SELECT chat_id, user_message_id, run_id
        FROM conversation_turns WHERE turn_id = ?
        """,
        (turn_id,),
    ).fetchone()
    if existing is not None and tuple(existing) != (
        chat_id,
        user_message_id,
        run_id,
    ):
        raise RuntimeError(
            "Conversation turn identity cannot be rebound to another "
            f"chat, user message, or run: {turn_id}"
        )
    conflicting_user = conn.execute(
        """
        SELECT turn_id, run_id FROM conversation_turns
        WHERE chat_id = ? AND user_message_id = ?
        """,
        (chat_id, user_message_id),
    ).fetchone()
    if conflicting_user is not None and tuple(conflicting_user) != (
        turn_id,
        run_id,
    ):
        raise RuntimeError(
            f"User message {user_message_id} already belongs to another run."
        )
    conn.execute(
        """
        INSERT INTO conversation_threads
            (chat_id, format_version, seed_kind, seeded_at,
             next_item_position, created_at, updated_at)
        VALUES (?, 1, 'native', NULL, 0, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET updated_at = excluded.updated_at
        """,
        (chat_id, now, now),
    )
    conn.execute(
        """
        INSERT INTO conversation_turns
            (turn_id, chat_id, user_message_id, run_id, status,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, 'queued', ?, ?)
        ON CONFLICT(turn_id) DO UPDATE SET
            updated_at = excluded.updated_at
        """,
        (turn_id, chat_id, user_message_id, run_id, now, now),
    )


class ConversationStore:
    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        initialize_app_db(self.db_path)

    @staticmethod
    def child_scope(child_run_id: str) -> str:
        return f"subagent:{child_run_id}"

    def create_turn(
        self,
        *,
        chat_id: str,
        turn_id: str,
        user_message_id: str,
        run_id: str,
    ) -> None:
        now = _now_ms()
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            create_turn_row(
                conn,
                chat_id=chat_id,
                turn_id=turn_id,
                user_message_id=user_message_id,
                run_id=run_id,
                now=now,
            )

    def set_turn_status(self, turn_id: str, status: str) -> None:
        if status not in {"queued", "running", "complete", "error", "cancelled"}:
            raise ValueError(f"Unsupported conversation turn status: {status}")
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM conversation_turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Conversation turn {turn_id} does not exist")
            current_status = str(row[0])
            if current_status == status:
                return
            allowed_transitions = {
                "queued": {"running", "complete", "error", "cancelled"},
                "running": {"complete", "error", "cancelled"},
                "complete": set(),
                "error": set(),
                "cancelled": set(),
            }
            if status not in allowed_transitions[current_status]:
                raise RuntimeError(
                    f"Conversation turn {turn_id} cannot transition "
                    f"from {current_status} to {status}"
                )
            conn.execute(
                "UPDATE conversation_turns SET status = ?, updated_at = ? WHERE turn_id = ?",
                (status, _now_ms(), turn_id),
            )

    def get_run_status(self, run_id: str) -> str | None:
        with get_db_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM conversation_turns WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def ensure_seeded(
        self,
        chat_id: str,
        seed_items: list[tuple[str, ConversationItem]],
    ) -> bool:
        """Seed pre-cutover UI history once. Returns True when this call seeded it."""
        now = _now_ms()
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO conversation_threads
                    (chat_id, format_version, seed_kind, seeded_at,
                     next_item_position, created_at, updated_at)
                VALUES (?, 1, 'native', NULL, 0, ?, ?)
                ON CONFLICT(chat_id) DO NOTHING
                """,
                (chat_id, now, now),
            )
            row = conn.execute(
                "SELECT seeded_at, next_item_position FROM conversation_threads WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Conversation thread was not created for {chat_id}")
            if row[0] is not None:
                return False

            next_position = int(row[1])
            for source_message_id, raw_item in seed_items:
                item = _item_dict(raw_item)
                key = _stable_item_key(
                    item,
                    run_id=None,
                    source_message_id=source_message_id,
                    fallback_suffix="0",
                )
                inserted = self._insert_or_update_item(
                    conn,
                    chat_id=chat_id,
                    scope_key=MAIN_SCOPE,
                    run_id=None,
                    turn_id=None,
                    source_message_id=source_message_id,
                    response_id=None,
                    response_index=None,
                    position=next_position,
                    item=item,
                    dedupe_key=key,
                    source="legacy_seed",
                    replayable=True,
                )
                if inserted:
                    next_position += 1

            conn.execute(
                """
                UPDATE conversation_threads
                SET seeded_at = ?, seed_kind = ?, next_item_position = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (
                    now,
                    "legacy_lossy" if seed_items else "native",
                    next_position,
                    now,
                    chat_id,
                ),
            )
        return True

    def _allocate_positions(self, conn: Any, chat_id: str, count: int) -> int:
        row = conn.execute(
            "SELECT next_item_position FROM conversation_threads WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Conversation thread does not exist for {chat_id}")
        start = int(row[0])
        conn.execute(
            """
            UPDATE conversation_threads
            SET next_item_position = ?, updated_at = ?
            WHERE chat_id = ?
            """,
            (start + count, _now_ms(), chat_id),
        )
        return start

    def _insert_or_update_item(
        self,
        conn: Any,
        *,
        chat_id: str,
        scope_key: str,
        run_id: str | None,
        turn_id: str | None,
        source_message_id: str | None,
        response_id: str | None,
        response_index: int | None,
        position: int,
        item: Mapping[str, Any],
        dedupe_key: str,
        source: str,
        replayable: bool,
    ) -> bool:
        existing = conn.execute(
            """
            SELECT id FROM conversation_items
            WHERE chat_id = ? AND scope_key = ? AND rail = ? AND dedupe_key = ?
            """,
            (chat_id, scope_key, OPENAI_RAIL, dedupe_key),
        ).fetchone()
        item_json = _json_text(item)
        fields = (
            _item_type(item),
            item.get("role") if isinstance(item.get("role"), str) else None,
            _call_id(item),
            _tool_name(item),
            _content_text(item),
            item_json,
        )
        if existing is not None:
            # Hooks may stage a call/output before the SDK Session later
            # supplies its final replay form. Keep the original position and
            # update the payload to the newest exact representation.
            conn.execute(
                """
                UPDATE conversation_items
                SET run_id = COALESCE(run_id, ?),
                    turn_id = COALESCE(turn_id, ?),
                    response_id = COALESCE(response_id, ?),
                    response_index = COALESCE(response_index, ?),
                    item_type = ?, role = ?, call_id = ?, tool_name = ?,
                    content_text = ?, item_json = ?,
                    replayable = CASE WHEN replayable = 1 OR ? = 1 THEN 1 ELSE 0 END,
                    active = 1
                WHERE id = ?
                """,
                (
                    run_id,
                    turn_id,
                    response_id,
                    response_index,
                    *fields,
                    1 if replayable else 0,
                    existing[0],
                ),
            )
            return False

        conn.execute(
            """
            INSERT INTO conversation_items
                (chat_id, scope_key, rail, run_id, turn_id, source_message_id,
                 response_id, response_index, position, item_type, role,
                 call_id, tool_name, content_text, item_json, dedupe_key,
                 source, replayable, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                chat_id,
                scope_key,
                OPENAI_RAIL,
                run_id,
                turn_id,
                source_message_id,
                response_id,
                response_index,
                position,
                *fields,
                dedupe_key,
                source,
                1 if replayable else 0,
                _now_ms(),
            ),
        )
        return True

    def append_items(
        self,
        *,
        chat_id: str,
        scope_key: str,
        run_id: str | None,
        turn_id: str | None,
        items: list[ConversationItem | Mapping[str, Any]],
        source: str,
        replayable: bool,
        source_message_id: str | None = None,
        response_id: str | None = None,
        dedupe_prefix: str = "batch",
        reserved_positions: Mapping[str, int] | None = None,
    ) -> list[StoredConversationItem]:
        if not items:
            return []
        normalized = [_item_dict(item) for item in items]
        reserved_positions = reserved_positions or {}
        keyed_items = [
            (
                item,
                _stable_item_key(
                    item,
                    run_id=run_id,
                    source_message_id=source_message_id if index == 0 else None,
                    fallback_suffix=f"{dedupe_prefix}:{index}",
                ),
            )
            for index, item in enumerate(normalized)
        ]
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            keys = list(dict.fromkeys(key for _, key in keyed_items))
            placeholders = ",".join("?" for _ in keys)
            existing_positions = {
                str(key): int(position)
                for key, position in conn.execute(
                    f"""
                    SELECT dedupe_key, position FROM conversation_items
                    WHERE chat_id = ? AND scope_key = ? AND rail = ?
                      AND dedupe_key IN ({placeholders})
                    """,
                    (chat_id, scope_key, OPENAI_RAIL, *keys),
                ).fetchall()
            }
            new_keys = {
                key
                for _, key in keyed_items
                if key not in existing_positions and key not in reserved_positions
            }
            next_position = self._allocate_positions(conn, chat_id, len(new_keys))
            assigned_positions = dict(existing_positions)
            stored: list[StoredConversationItem] = []
            for index, (item, key) in enumerate(keyed_items):
                position = assigned_positions.get(key, reserved_positions.get(key))
                if position is None:
                    position = next_position
                    next_position += 1
                assigned_positions[key] = position
                self._insert_or_update_item(
                    conn,
                    chat_id=chat_id,
                    scope_key=scope_key,
                    run_id=run_id,
                    turn_id=turn_id,
                    source_message_id=source_message_id if index == 0 else None,
                    response_id=response_id,
                    response_index=index,
                    position=position,
                    item=item,
                    dedupe_key=key,
                    source=source,
                    replayable=replayable,
                )
                row = conn.execute(
                    """
                    SELECT id, position FROM conversation_items
                    WHERE chat_id = ? AND scope_key = ? AND rail = ? AND dedupe_key = ?
                    """,
                    (chat_id, scope_key, OPENAI_RAIL, key),
                ).fetchone()
                if row is not None:
                    stored.append(StoredConversationItem(int(row[0]), int(row[1]), key))
            return stored

    def stage_model_response(
        self,
        *,
        chat_id: str,
        scope_key: str,
        run_id: str,
        turn_id: str,
        response_id: str | None,
        items: list[Any],
        replayable: bool,
    ) -> set[str]:
        """Atomically store a model output and reserve its tool-result slots."""
        normalized = [_item_dict(item) for item in items]
        if not normalized:
            return set()
        now = _now_ms()
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            turn_status = conn.execute(
                "SELECT status FROM conversation_turns WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
            if turn_status is None:
                raise RuntimeError(f"Conversation turn {turn_id} does not exist")
            if turn_status[0] not in {"queued", "running"}:
                raise RuntimeError(
                    f"Cannot stage model output for terminal turn {turn_id}"
                )
            call_items = [item for item in normalized if _item_type(item) == "function_call"]
            base = self._allocate_positions(conn, chat_id, len(normalized) + len(call_items))
            call_ids: set[str] = set()
            call_ordinal = 0
            for index, item in enumerate(normalized):
                key = _stable_item_key(
                    item,
                    run_id=run_id,
                    source_message_id=None,
                    fallback_suffix=f"response:{response_id or 'none'}:{index}",
                )
                self._insert_or_update_item(
                    conn,
                    chat_id=chat_id,
                    scope_key=scope_key,
                    run_id=run_id,
                    turn_id=turn_id,
                    source_message_id=None,
                    response_id=response_id,
                    response_index=index,
                    position=base + index,
                    item=item,
                    dedupe_key=key,
                    source="model_response",
                    replayable=replayable,
                )
                if _item_type(item) != "function_call":
                    continue
                call_id = _call_id(item)
                name = _tool_name(item)
                if not call_id or not name:
                    continue
                call_ids.add(call_id)
                output_position = base + len(normalized) + call_ordinal
                call_ordinal += 1
                arguments = item.get("arguments", "{}")
                arguments_json = arguments if isinstance(arguments, str) else _json_text(arguments)
                conn.execute(
                    """
                    INSERT INTO tool_executions
                        (chat_id, scope_key, run_id, turn_id, response_id,
                         response_index, output_position, call_id, tool_name,
                         arguments_json, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT(chat_id, scope_key, call_id) DO UPDATE SET
                        response_id = excluded.response_id,
                        response_index = excluded.response_index,
                        output_position = excluded.output_position,
                        arguments_json = excluded.arguments_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        chat_id,
                        scope_key,
                        run_id,
                        turn_id,
                        response_id,
                        index,
                        output_position,
                        call_id,
                        name,
                        arguments_json,
                        now,
                        now,
                    ),
                )
            return call_ids

    def mark_tool_started(self, *, chat_id: str, scope_key: str, call_id: str) -> None:
        now = _now_ms()
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE tool_executions
                SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ?
                WHERE chat_id = ? AND scope_key = ? AND call_id = ?
                  AND status IN ('pending', 'running')
                  AND EXISTS (
                      SELECT 1 FROM conversation_turns AS turn
                      WHERE turn.turn_id = tool_executions.turn_id
                        AND turn.status IN ('queued', 'running')
                  )
                """,
                (now, now, chat_id, scope_key, call_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Tool call {call_id} is not durably staged for {chat_id}/{scope_key}"
                )

    def complete_tool(
        self,
        *,
        chat_id: str,
        scope_key: str,
        run_id: str,
        turn_id: str,
        call_id: str,
        raw_output_item: Mapping[str, Any],
    ) -> int:
        item = _item_dict(raw_output_item)
        now = _now_ms()
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            execution = conn.execute(
                """
                SELECT output_position, response_id FROM tool_executions
                WHERE chat_id = ? AND scope_key = ? AND call_id = ?
                """,
                (chat_id, scope_key, call_id),
            ).fetchone()
            if execution is None:
                raise RuntimeError(f"Tool output {call_id} has no staged execution")
            output_position = int(execution[0])
            key = _stable_item_key(
                item,
                run_id=run_id,
                source_message_id=None,
                fallback_suffix=f"tool-output:{call_id}",
            )
            self._insert_or_update_item(
                conn,
                chat_id=chat_id,
                scope_key=scope_key,
                run_id=run_id,
                turn_id=turn_id,
                source_message_id=None,
                response_id=execution[1],
                response_index=None,
                position=output_position,
                item=item,
                dedupe_key=key,
                source="tool_output",
                replayable=scope_key == MAIN_SCOPE,
            )
            conn.execute(
                """
                UPDATE tool_executions
                SET status = 'completed', result_json = ?, completed_at = ?, updated_at = ?
                WHERE chat_id = ? AND scope_key = ? AND call_id = ?
                """,
                (_json_text(item), now, now, chat_id, scope_key, call_id),
            )
            row = conn.execute(
                """
                SELECT id FROM conversation_items
                WHERE chat_id = ? AND scope_key = ? AND rail = ? AND dedupe_key = ?
                """,
                (chat_id, scope_key, OPENAI_RAIL, key),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Tool output {call_id} was not persisted")
            return int(row[0])

    def _finalize_unfinished_tools_in_transaction(
        self,
        conn: Any,
        *,
        run_id: str,
        scope_key: str,
    ) -> None:
        now = _now_ms()
        rows = conn.execute(
            """
            SELECT chat_id, turn_id, response_id, output_position, call_id,
                   status
            FROM tool_executions
            WHERE run_id = ? AND scope_key = ? AND status IN ('pending', 'running')
            ORDER BY response_index ASC, id ASC
            """,
            (run_id, scope_key),
        ).fetchall()
        for (
            chat_id,
            turn_id,
            response_id,
            output_position,
            call_id,
            status,
        ) in rows:
            final_status = "unknown" if status == "running" else "cancelled"
            error = (
                "Execution ended after the tool started without a durable result; "
                "its external effects are unknown. Do not retry it automatically."
                if status == "running"
                else "Execution was cancelled before the tool started."
            )
            output_item = {
                "type": "function_call_output",
                "call_id": call_id,
                "output": _json_text(
                    {
                        "status": final_status,
                        "error": error,
                    }
                ),
            }
            position = output_position
            if position is None:
                position = self._allocate_positions(conn, chat_id, 1)
            key = _stable_item_key(
                output_item,
                run_id=run_id,
                source_message_id=None,
                fallback_suffix=f"tool-recovery:{call_id}",
            )
            self._insert_or_update_item(
                conn,
                chat_id=chat_id,
                scope_key=scope_key,
                run_id=run_id,
                turn_id=turn_id,
                source_message_id=None,
                response_id=response_id,
                response_index=None,
                position=int(position),
                item=output_item,
                dedupe_key=key,
                source="tool_recovery",
                replayable=scope_key == MAIN_SCOPE,
            )
            conn.execute(
                """
                UPDATE tool_executions
                SET status = ?, result_json = ?, error = ?,
                    completed_at = ?, updated_at = ?
                WHERE chat_id = ? AND scope_key = ? AND call_id = ?
                """,
                (
                    final_status,
                    _json_text(output_item),
                    error,
                    now,
                    now,
                    chat_id,
                    scope_key,
                    call_id,
                ),
            )

        # Keep this guard for rows added by a future item type that cannot yet
        # be materialized as a function_call_output.
        conn.execute(
            """
            UPDATE tool_executions
            SET status = CASE WHEN status = 'running' THEN 'unknown' ELSE 'cancelled' END,
                error = CASE
                    WHEN status = 'running' THEN 'Execution ended without a durable terminal output.'
                    ELSE 'Execution was not started.'
                END,
                completed_at = ?, updated_at = ?
            WHERE run_id = ? AND scope_key = ? AND status IN ('pending', 'running')
            """,
            (now, now, run_id, scope_key),
        )

    def finalize_unfinished_tools(self, *, run_id: str, scope_key: str) -> None:
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._finalize_unfinished_tools_in_transaction(
                conn,
                run_id=run_id,
                scope_key=scope_key,
            )

    def finalize_turn_unfinished_tools(self, *, turn_id: str) -> None:
        """Finalize parent and nested tool scopes in one terminal barrier."""
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            scopes = conn.execute(
                """
                SELECT DISTINCT run_id, scope_key FROM tool_executions
                WHERE turn_id = ? AND status IN ('pending', 'running')
                ORDER BY id
                """,
                (turn_id,),
            ).fetchall()
            for execution_run_id, execution_scope in scopes:
                self._finalize_unfinished_tools_in_transaction(
                    conn,
                    run_id=execution_run_id,
                    scope_key=execution_scope,
                )

    def finish_turn(
        self,
        *,
        turn_id: str,
        run_id: str,
        status: str,
        payload: Mapping[str, Any],
    ) -> int:
        """Atomically finalize tools, record lifecycle, and close one turn."""
        event_types = {
            "complete": "run.completed",
            "error": "run.error",
            "cancelled": "run.cancelled",
        }
        event_type = event_types.get(status)
        if event_type is None:
            raise ValueError(f"Unsupported terminal conversation status: {status}")

        now = _now_ms()
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            turn = conn.execute(
                """
                SELECT chat_id, run_id, status FROM conversation_turns
                WHERE turn_id = ?
                """,
                (turn_id,),
            ).fetchone()
            if turn is None:
                raise RuntimeError(f"Conversation turn {turn_id} does not exist")
            chat_id, stored_run_id, stored_status = turn
            if stored_run_id != run_id:
                raise RuntimeError(
                    f"Conversation turn {turn_id} belongs to run {stored_run_id}, not {run_id}"
                )
            if stored_status in {"complete", "error", "cancelled"}:
                if stored_status != status:
                    raise RuntimeError(
                        f"Conversation turn {turn_id} is already {stored_status}"
                    )
                existing = conn.execute(
                    """
                    SELECT id FROM conversation_events
                    WHERE run_id = ? AND scope_key = ? AND event_type = ?
                    ORDER BY sequence DESC LIMIT 1
                    """,
                    (run_id, MAIN_SCOPE, event_type),
                ).fetchone()
                if existing is None:
                    raise RuntimeError(
                        f"Conversation turn {turn_id} is terminal without {event_type}"
                    )
                return int(existing[0])

            scopes = conn.execute(
                """
                SELECT run_id, scope_key FROM tool_executions
                WHERE turn_id = ? AND status IN ('pending', 'running')
                GROUP BY run_id, scope_key
                ORDER BY MIN(id)
                """,
                (turn_id,),
            ).fetchall()
            for execution_run_id, execution_scope in scopes:
                self._finalize_unfinished_tools_in_transaction(
                    conn,
                    run_id=execution_run_id,
                    scope_key=execution_scope,
                )

            sequence = int(
                conn.execute(
                    """
                    SELECT COALESCE(MAX(sequence), -1) + 1
                    FROM conversation_events WHERE run_id = ? AND scope_key = ?
                    """,
                    (run_id, MAIN_SCOPE),
                ).fetchone()[0]
            )
            cursor = conn.execute(
                """
                INSERT INTO conversation_events
                    (chat_id, run_id, turn_id, scope_key, rail, sequence,
                     event_type, item_type, call_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'application_event', NULL, ?, ?)
                """,
                (
                    chat_id,
                    run_id,
                    turn_id,
                    MAIN_SCOPE,
                    OPENAI_RAIL,
                    sequence,
                    event_type,
                    _json_text(payload),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE conversation_turns SET status = ?, updated_at = ?
                WHERE turn_id = ?
                """,
                (status, now, turn_id),
            )
            return int(cursor.lastrowid)

    def recover_stale_run(self, run_id: str, *, error: str) -> bool:
        """Atomically repair canonical state left by a process restart."""
        now = _now_ms()
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            turn = conn.execute(
                """
                SELECT turn_id, chat_id, status FROM conversation_turns
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if turn is None:
                # Child/nested executions have their own run IDs but share the
                # parent's turn. Older or partially written rows may also have
                # no turn at all. Recover every matching scope rather than
                # assuming the main scope.
                scopes = conn.execute(
                    """
                    SELECT DISTINCT run_id, scope_key FROM tool_executions
                    WHERE run_id = ? AND status IN ('pending', 'running')
                    """,
                    (run_id,),
                ).fetchall()
                for execution_run_id, execution_scope in scopes:
                    self._finalize_unfinished_tools_in_transaction(
                        conn,
                        run_id=execution_run_id,
                        scope_key=execution_scope,
                    )
                return bool(scopes)

            turn_id, chat_id, status = turn
            scopes = conn.execute(
                """
                SELECT DISTINCT run_id, scope_key FROM tool_executions
                WHERE turn_id = ? AND status IN ('pending', 'running')
                """,
                (turn_id,),
            ).fetchall()
            for execution_run_id, execution_scope in scopes:
                self._finalize_unfinished_tools_in_transaction(
                    conn,
                    run_id=execution_run_id,
                    scope_key=execution_scope,
                )

            # A terminal turn can still own unfinished executions when its
            # best-effort error/cancellation cleanup failed. The tool journal
            # must be repaired even though the turn status no longer needs a
            # transition.
            if status not in {"queued", "running"}:
                return bool(scopes)

            conn.execute(
                """
                UPDATE conversation_turns
                SET status = 'error', updated_at = ? WHERE turn_id = ?
                """,
                (now, turn_id),
            )
            sequence = int(
                conn.execute(
                    """
                    SELECT COALESCE(MAX(sequence), -1) + 1
                    FROM conversation_events WHERE run_id = ? AND scope_key = ?
                    """,
                    (run_id, MAIN_SCOPE),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO conversation_events
                    (chat_id, run_id, turn_id, scope_key, rail, sequence,
                     event_type, item_type, call_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'run.recovered_error',
                        'application_event', NULL, ?, ?)
                """,
                (
                    chat_id,
                    run_id,
                    turn_id,
                    MAIN_SCOPE,
                    OPENAI_RAIL,
                    sequence,
                    _json_text(
                        {
                            "runId": run_id,
                            "turnId": turn_id,
                            "error": error,
                            "recovered": True,
                        }
                    ),
                    now,
                ),
            )
            return True

    def recover_stale_runs(
        self,
        *,
        error: str,
        chat_id: str | None = None,
    ) -> list[str]:
        """Recover nonterminal turns and every unfinished tool execution."""
        turn_query = (
            "SELECT run_id FROM conversation_turns "
            "WHERE status IN ('queued', 'running') AND run_id IS NOT NULL"
        )
        params: tuple[str, ...] = ()
        if chat_id is not None:
            turn_query += " AND chat_id = ?"
            params = (chat_id,)
        turn_query += " ORDER BY created_at, turn_id"
        with get_db_connection(self.db_path) as conn:
            run_ids = [
                str(row[0]) for row in conn.execute(turn_query, params).fetchall()
            ]
            execution_query = """
                SELECT DISTINCT COALESCE(parent.run_id, execution.run_id)
                FROM tool_executions AS execution
                LEFT JOIN conversation_turns AS parent
                    ON parent.turn_id = execution.turn_id
                WHERE execution.status IN ('pending', 'running')
            """
            execution_params: tuple[str, ...] = ()
            if chat_id is not None:
                execution_query += " AND execution.chat_id = ?"
                execution_params = (chat_id,)
            execution_query += " ORDER BY 1"
            for row in conn.execute(execution_query, execution_params).fetchall():
                candidate = str(row[0])
                if candidate not in run_ids:
                    run_ids.append(candidate)

        recovered: list[str] = []
        for stale_run_id in run_ids:
            if self.recover_stale_run(stale_run_id, error=error):
                recovered.append(stale_run_id)
        return recovered

    def list_items(
        self,
        *,
        chat_id: str,
        scope_key: str = MAIN_SCOPE,
        exclude_turn_id: str | None = None,
        limit: int | None = None,
    ) -> list[ConversationItem]:
        where = [
            "chat_id = ?",
            "scope_key = ?",
            "rail = ?",
            "active = 1",
            "replayable = 1",
        ]
        params: list[Any] = [chat_id, scope_key, OPENAI_RAIL]
        if exclude_turn_id is not None:
            where.append("(turn_id IS NULL OR turn_id != ?)")
            params.append(exclude_turn_id)
        query = (
            "SELECT item_json FROM conversation_items WHERE "
            + " AND ".join(where)
            + " ORDER BY position ASC"
        )
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        items: list[ConversationItem] = []
        for (payload,) in rows:
            try:
                item = json.loads(payload)
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Corrupt canonical conversation item for {chat_id}") from exc
            if isinstance(item, dict):
                items.append(item)
        if limit is not None and limit >= 0:
            return items[-limit:] if limit else []
        return items

    def list_openai_context_items(
        self,
        *,
        chat_id: str,
        scope_key: str = MAIN_SCOPE,
        exclude_turn_id: str | None = None,
        item_limit: int | None = None,
        max_text_chars: int = DEFAULT_OPENAI_HISTORY_TEXT_CHARS,
        max_media_chars: int = DEFAULT_OPENAI_HISTORY_MEDIA_CHARS,
    ) -> list[ConversationItem]:
        """Select a bounded, protocol-valid suffix for an OpenAI request.

        All canonical rows remain in SQLite. Only the materialized request is
        windowed, and atomic reasoning/call/output groups are never split.
        """
        items = self.list_items(
            chat_id=chat_id,
            scope_key=scope_key,
            exclude_turn_id=exclude_turn_id,
        )
        selected_reversed: list[list[ConversationItem]] = []
        text_chars = 0
        media_chars = 0
        item_count = 0
        for group in reversed(_openai_atomic_groups(items)):
            group_text = 0
            group_media = 0
            for raw_item in group:
                item_text, item_media = _item_budget(_item_dict(raw_item))
                group_text += item_text
                group_media += item_media
            if (
                text_chars + group_text > max(0, max_text_chars)
                or media_chars + group_media > max(0, max_media_chars)
                or (
                    item_limit is not None
                    and item_limit >= 0
                    and item_count + len(group) > item_limit
                )
            ):
                break
            selected_reversed.append(group)
            text_chars += group_text
            media_chars += group_media
            item_count += len(group)

        selected: list[ConversationItem] = []
        for group in reversed(selected_reversed):
            selected.extend(group)
        return selected

    def append_event(
        self,
        *,
        chat_id: str,
        run_id: str,
        turn_id: str | None,
        scope_key: str,
        event_type: str,
        payload: Mapping[str, Any],
        item_type: str | None = None,
        call_id: str | None = None,
    ) -> int:
        now = _now_ms()
        (
            provider_item_id,
            output_index,
            content_index,
            provider_sequence,
        ) = _event_projection(payload)
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            sequence = int(
                conn.execute(
                    """
                    SELECT COALESCE(MAX(sequence), -1) + 1
                    FROM conversation_events WHERE run_id = ? AND scope_key = ?
                    """,
                    (run_id, scope_key),
                ).fetchone()[0]
            )
            cursor = conn.execute(
                """
                INSERT INTO conversation_events
                    (chat_id, run_id, turn_id, scope_key, rail, sequence,
                     event_type, item_type, call_id, provider_item_id,
                     output_index, content_index, provider_sequence,
                     payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    run_id,
                    turn_id,
                    scope_key,
                    OPENAI_RAIL,
                    sequence,
                    event_type,
                    item_type,
                    call_id,
                    provider_item_id,
                    output_index,
                    content_index,
                    provider_sequence,
                    _json_text(payload),
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def pop_item(
        self,
        chat_id: str,
        scope_key: str = MAIN_SCOPE,
    ) -> ConversationItem | None:
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id, item_json FROM conversation_items
                WHERE chat_id = ? AND scope_key = ? AND rail = ? AND active = 1
                ORDER BY position DESC LIMIT 1
                """,
                (chat_id, scope_key, OPENAI_RAIL),
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE conversation_items SET active = 0 WHERE id = ?", (row[0],))
            value = json.loads(row[1])
            return value if isinstance(value, dict) else None

    def clear_items(self, chat_id: str, scope_key: str = MAIN_SCOPE) -> None:
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE conversation_items SET active = 0
                WHERE chat_id = ? AND scope_key = ? AND rail = ? AND active = 1
                """,
                (chat_id, scope_key, OPENAI_RAIL),
            )


__all__ = [
    "ConversationItem",
    "MAIN_SCOPE",
    "OPENAI_RAIL",
    "ConversationStore",
]
