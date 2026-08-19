from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from pydantic import ValidationError

from aios_core.conversation_store import create_turn_row
from aios_core.db import DB_PATH, get_db_connection, initialize_app_db
from aios_core.sessions import append_assistant_event
from aios_core.workspace import get_runs_dir
from server.types.run import (
    Run,
    RunCreateRequest,
    RunEvent,
    RunKind,
    RunSnapshot,
    RunStatus,
)

log = logging.getLogger(__name__)


class RunStore:
    def create_run(self, request: RunCreateRequest, *, user_id: str | None = None) -> Run:
        raise NotImplementedError

    def get_run(self, run_id: str, *, user_id: str | None = None) -> Run | None:
        raise NotImplementedError

    def save_run(self, run: Run) -> Run:
        raise NotImplementedError

    def append_event(self, run_id: str, event: RunEvent) -> RunEvent:
        raise NotImplementedError

    def record_event(
        self,
        run_id: str,
        event: RunEvent,
        *,
        status: RunStatus,
        preview: str | None = None,
        active_step: str | None = None,
    ) -> tuple[RunEvent, RunSnapshot]:
        """Persist an event and its snapshot projection.

        File storage cannot make these writes atomic. Database stores override
        this method so reconnect cursors and visible run status commit together.
        """

        persisted_event = self.append_event(run_id, event)
        snapshot = self.save_snapshot(
            run_id,
            status=status,
            last_sequence=persisted_event.sequence,
            preview=preview,
            active_step=active_step,
        )
        return persisted_event, snapshot

    def save_snapshot(
        self,
        run_id: str,
        *,
        status: RunStatus,
        last_sequence: int,
        preview: str | None = None,
        active_step: str | None = None,
    ) -> RunSnapshot:
        raise NotImplementedError

    def get_snapshot(self, run_id: str) -> RunSnapshot | None:
        raise NotImplementedError

    def list_snapshots(
        self,
        user_id: str | None = None,
        statuses: list[RunStatus] | None = None,
        kinds: list[RunKind] | None = None,
        limit: int | None = None,
    ) -> list[RunSnapshot]:
        raise NotImplementedError

    def list_events_after(
        self,
        run_id: str,
        sequence: int,
        *,
        user_id: str | None = None,
    ) -> list[RunEvent]:
        raise NotImplementedError

    def project_chat_state(self, run_id: str, chat_id: str, event: RunEvent) -> None:
        raise NotImplementedError


class FileRunStore(RunStore):
    def __init__(
        self,
        metadata_dir: str | Path | None = None,
        snapshots_dir: str | Path | None = None,
        events_dir: str | Path | None = None,
        *,
        create_directories: bool = True,
    ) -> None:
        legacy_runs_dir = get_runs_dir()
        self._metadata_dir = Path(metadata_dir or legacy_runs_dir / "metadata")
        self._snapshots_dir = Path(snapshots_dir or legacy_runs_dir / "snapshots")
        self._events_dir = Path(events_dir or legacy_runs_dir / "events")
        if create_directories:
            self._ensure_directories()

    def create_run(self, request: RunCreateRequest, *, user_id: str | None = None) -> Run:
        now = int(time.time() * 1000)
        run = Run(
            id=str(uuid.uuid4()),
            userId=user_id,
            kind=request.kind,
            status="queued",
            createdAt=now,
            updatedAt=now,
            chatId=request.chatId,
            sourceId=request.sourceId,
            turnId=request.turnId,
        )
        self.save_run(run)
        return run

    def get_run(self, run_id: str, *, user_id: str | None = None) -> Run | None:
        path = self._metadata_path(run_id)
        if not path.exists():
            return None
        run = Run.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if user_id is not None and run.userId != user_id:
            return None
        return run

    def save_run(self, run: Run) -> Run:
        self._metadata_path(run.id).write_text(
            json.dumps(run.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        return run

    def append_event(self, run_id: str, event: RunEvent) -> RunEvent:
        run = self._ensure_run_exists(run_id)
        next_sequence = self._get_last_sequence(run_id) + 1
        persisted_event = event.model_copy(
            update={
                "sequence": next_sequence,
                "userId": event.userId if event.userId is not None else run.userId,
            }
        )
        with self._events_path(run_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(persisted_event.model_dump(mode="json")))
            handle.write("\n")
        return persisted_event

    def save_snapshot(
        self,
        run_id: str,
        *,
        status: RunStatus,
        last_sequence: int,
        preview: str | None = None,
        active_step: str | None = None,
    ) -> RunSnapshot:
        run = self._ensure_run_exists(run_id)
        updated_at = int(time.time() * 1000)
        snapshot = RunSnapshot(
            runId=run.id,
            userId=run.userId,
            kind=run.kind,
            status=status,
            updatedAt=updated_at,
            chatId=run.chatId,
            lastSequence=last_sequence,
            preview=preview,
            activeStep=active_step,
        )
        self._snapshot_path(run_id).write_text(
            json.dumps(snapshot.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        self.save_run(run.model_copy(update={"status": status, "updatedAt": updated_at}))
        return snapshot

    def get_snapshot(self, run_id: str) -> RunSnapshot | None:
        path = self._snapshot_path(run_id)
        if not path.exists():
            return None
        return RunSnapshot.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def list_snapshots(
        self,
        user_id: str | None = None,
        statuses: list[RunStatus] | None = None,
        kinds: list[RunKind] | None = None,
        limit: int | None = None,
    ) -> list[RunSnapshot]:
        allowed_statuses = set(statuses or [])
        allowed_kinds = set(kinds or [])
        snapshots: list[RunSnapshot] = []

        for path in self._snapshots_dir.glob("*.json"):
            try:
                snapshot = RunSnapshot.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except (ValidationError, json.JSONDecodeError, OSError):
                log.warning("Skipping unreadable run snapshot at %s", path)
                continue
            if user_id is not None and snapshot.userId != user_id:
                continue
            if allowed_statuses and snapshot.status not in allowed_statuses:
                continue
            if allowed_kinds and snapshot.kind not in allowed_kinds:
                continue
            snapshots.append(snapshot)

        snapshots.sort(key=lambda item: item.updatedAt, reverse=True)
        if limit is not None:
            return snapshots[:limit]
        return snapshots

    def list_events_after(
        self,
        run_id: str,
        sequence: int,
        *,
        user_id: str | None = None,
    ) -> list[RunEvent]:
        if user_id is not None and self.get_run(run_id, user_id=user_id) is None:
            return []

        path = self._events_path(run_id)
        if not path.exists():
            return []

        events: list[RunEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = RunEvent.model_validate(json.loads(line))
            if event.sequence > sequence:
                events.append(event)
        return events

    def project_chat_state(self, run_id: str, chat_id: str, event: RunEvent) -> None:
        llm_event = _run_event_to_chat_event(event)
        if llm_event is None:
            return

        append_assistant_event(chat_id, run_id, llm_event)

    def _ensure_directories(self) -> None:
        self._metadata_dir.mkdir(parents=True, exist_ok=True)
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._events_dir.mkdir(parents=True, exist_ok=True)

    def _metadata_path(self, run_id: str) -> Path:
        return self._metadata_dir / f"{run_id}.json"

    def _snapshot_path(self, run_id: str) -> Path:
        return self._snapshots_dir / f"{run_id}.json"

    def _events_path(self, run_id: str) -> Path:
        return self._events_dir / f"{run_id}.jsonl"

    def _ensure_run_exists(self, run_id: str) -> Run:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"Run {run_id} does not exist.")
        return run

    def _get_last_sequence(self, run_id: str) -> int:
        snapshot = self.get_snapshot(run_id)
        if snapshot is not None:
            return snapshot.lastSequence

        events = self.list_events_after(run_id, 0)
        if not events:
            return 0
        return events[-1].sequence


class SQLiteRunStore(RunStore):
    """SQLite-backed scheduling state and exact reconnect event stream."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        initialize_app_db(self.db_path)

    def create_run(self, request: RunCreateRequest, *, user_id: str | None = None) -> Run:
        now = int(time.time() * 1000)
        run = Run(
            id=str(uuid.uuid4()),
            userId=user_id,
            kind=request.kind,
            status="queued",
            createdAt=now,
            updatedAt=now,
            chatId=request.chatId,
            sourceId=request.sourceId,
            turnId=request.turnId,
        )
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._insert_run(conn, run)
            if run.kind == "chat" and run.chatId and run.turnId:
                create_turn_row(
                    conn,
                    chat_id=run.chatId,
                    turn_id=run.turnId,
                    user_message_id=run.sourceId or run.turnId,
                    run_id=run.id,
                    now=now,
                )
        return run

    def get_run(self, run_id: str, *, user_id: str | None = None) -> Run | None:
        query = (
            "SELECT id, user_id, kind, status, created_at, updated_at, "
            "chat_id, source_id, turn_id FROM runs WHERE id = ?"
        )
        params: list[object] = [run_id]
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        with get_db_connection(self.db_path) as conn:
            row = conn.execute(query, params).fetchone()
        return self._run_from_row(row) if row is not None else None

    def save_run(self, run: Run) -> Run:
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT 1 FROM runs WHERE id = ?",
                (run.id,),
            ).fetchone()
            if existing is None:
                self._insert_run(conn, run)
            else:
                conn.execute(
                    """
                    UPDATE runs
                    SET user_id = ?, kind = ?, status = ?, chat_id = ?,
                        source_id = ?, turn_id = ?, created_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        run.userId,
                        run.kind,
                        run.status,
                        run.chatId,
                        run.sourceId,
                        run.turnId,
                        run.createdAt,
                        run.updatedAt,
                        run.id,
                    ),
                )
        return run

    def append_event(self, run_id: str, event: RunEvent) -> RunEvent:
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = self._select_run_row(conn, run_id)
            next_sequence = int(run_row[9]) + 1
            persisted_event = self._persist_event(
                conn,
                self._run_from_row(run_row[:9]),
                event,
                next_sequence,
            )
            conn.execute(
                "UPDATE runs SET last_sequence = ?, updated_at = ? WHERE id = ?",
                (next_sequence, persisted_event.createdAt, run_id),
            )
        return persisted_event

    def record_event(
        self,
        run_id: str,
        event: RunEvent,
        *,
        status: RunStatus,
        preview: str | None = None,
        active_step: str | None = None,
    ) -> tuple[RunEvent, RunSnapshot]:
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = self._select_run_row(conn, run_id)
            run = self._run_from_row(run_row[:9])
            next_sequence = int(run_row[9]) + 1
            persisted_event = self._persist_event(
                conn,
                run,
                event,
                next_sequence,
            )
            updated_at = int(time.time() * 1000)
            conn.execute(
                """
                UPDATE runs
                SET status = ?, last_sequence = ?, preview = ?, active_step = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    next_sequence,
                    preview,
                    active_step,
                    updated_at,
                    run_id,
                ),
            )
        return persisted_event, RunSnapshot(
            runId=run.id,
            userId=run.userId,
            kind=run.kind,
            status=status,
            updatedAt=updated_at,
            chatId=run.chatId,
            lastSequence=next_sequence,
            preview=preview,
            activeStep=active_step,
        )

    def save_snapshot(
        self,
        run_id: str,
        *,
        status: RunStatus,
        last_sequence: int,
        preview: str | None = None,
        active_step: str | None = None,
    ) -> RunSnapshot:
        updated_at = int(time.time() * 1000)
        with get_db_connection(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            run_row = self._select_run_row(conn, run_id)
            run = self._run_from_row(run_row[:9])
            conn.execute(
                """
                UPDATE runs
                SET status = ?, last_sequence = ?, preview = ?, active_step = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    last_sequence,
                    preview,
                    active_step,
                    updated_at,
                    run_id,
                ),
            )
        return RunSnapshot(
            runId=run.id,
            userId=run.userId,
            kind=run.kind,
            status=status,
            updatedAt=updated_at,
            chatId=run.chatId,
            lastSequence=last_sequence,
            preview=preview,
            activeStep=active_step,
        )

    def get_snapshot(self, run_id: str) -> RunSnapshot | None:
        with get_db_connection(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT id, user_id, kind, status, updated_at, chat_id,
                       last_sequence, preview, active_step
                FROM runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
        return self._snapshot_from_row(row) if row is not None else None

    def list_snapshots(
        self,
        user_id: str | None = None,
        statuses: list[RunStatus] | None = None,
        kinds: list[RunKind] | None = None,
        limit: int | None = None,
    ) -> list[RunSnapshot]:
        clauses: list[str] = []
        params: list[object] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if kinds:
            clauses.append(f"kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        query = (
            "SELECT id, user_id, kind, status, updated_at, chat_id, "
            "last_sequence, preview, active_step FROM runs"
        )
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, id ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._snapshot_from_row(row) for row in rows]

    def list_events_after(
        self,
        run_id: str,
        sequence: int,
        *,
        user_id: str | None = None,
    ) -> list[RunEvent]:
        if user_id is not None and self.get_run(run_id, user_id=user_id) is None:
            return []
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT event_json FROM run_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (run_id, sequence),
            ).fetchall()
        return [RunEvent.model_validate_json(row[0]) for row in rows]

    def project_chat_state(self, run_id: str, chat_id: str, event: RunEvent) -> None:
        llm_event = _run_event_to_chat_event(event)
        if llm_event is not None:
            append_assistant_event(chat_id, run_id, llm_event)

    def import_file_store(self, legacy_store: FileRunStore) -> int:
        """Copy legacy file runs once, without overwriting newer SQL state."""

        imported = 0
        for legacy_snapshot in legacy_store.list_snapshots():
            try:
                run = legacy_store.get_run(legacy_snapshot.runId)
                if run is None:
                    continue
                events = legacy_store.list_events_after(run.id, 0)
                with get_db_connection(self.db_path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO runs
                            (id, user_id, kind, status, chat_id, source_id, turn_id,
                             last_sequence, preview, active_step, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run.id,
                            run.userId,
                            run.kind,
                            legacy_snapshot.status,
                            run.chatId,
                            run.sourceId,
                            run.turnId,
                            legacy_snapshot.lastSequence,
                            legacy_snapshot.preview,
                            legacy_snapshot.activeStep,
                            run.createdAt,
                            legacy_snapshot.updatedAt,
                        ),
                    )
                    if cursor.rowcount == 0:
                        continue
                    for event in events:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO run_events
                                (run_id, sequence, event_json, created_at)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                run.id,
                                event.sequence,
                                event.model_dump_json(),
                                event.createdAt,
                            ),
                        )
                    imported += 1
            except Exception:
                log.exception(
                    "Skipping legacy run that could not be imported: %s",
                    legacy_snapshot.runId,
                )
        return imported

    @staticmethod
    def _insert_run(conn, run: Run) -> None:
        conn.execute(
            """
            INSERT INTO runs
                (id, user_id, kind, status, chat_id, source_id, turn_id,
                 last_sequence, preview, active_step, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?)
            """,
            (
                run.id,
                run.userId,
                run.kind,
                run.status,
                run.chatId,
                run.sourceId,
                run.turnId,
                run.createdAt,
                run.updatedAt,
            ),
        )

    @staticmethod
    def _select_run_row(conn, run_id: str):
        row = conn.execute(
            """
            SELECT id, user_id, kind, status, created_at, updated_at,
                   chat_id, source_id, turn_id, last_sequence
            FROM runs WHERE id = ?
            """,
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Run {run_id} does not exist.")
        return row

    @staticmethod
    def _persist_event(conn, run: Run, event: RunEvent, sequence: int) -> RunEvent:
        persisted_event = event.model_copy(
            update={
                "sequence": sequence,
                "kind": run.kind,
                "userId": event.userId if event.userId is not None else run.userId,
                "chatId": event.chatId if event.chatId is not None else run.chatId,
            }
        )
        conn.execute(
            """
            INSERT INTO run_events (run_id, sequence, event_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                run.id,
                sequence,
                persisted_event.model_dump_json(),
                persisted_event.createdAt,
            ),
        )
        return persisted_event

    @staticmethod
    def _run_from_row(row) -> Run:
        return Run(
            id=row[0],
            userId=row[1],
            kind=row[2],
            status=row[3],
            createdAt=row[4],
            updatedAt=row[5],
            chatId=row[6],
            sourceId=row[7],
            turnId=row[8],
        )

    @staticmethod
    def _snapshot_from_row(row) -> RunSnapshot:
        return RunSnapshot(
            runId=row[0],
            userId=row[1],
            kind=row[2],
            status=row[3],
            updatedAt=row[4],
            chatId=row[5],
            lastSequence=row[6],
            preview=row[7],
            activeStep=row[8],
        )


def _run_event_to_chat_event(event: RunEvent) -> dict[str, object] | None:
    data = event.event.data or {}
    payload = {
        "id": f"{event.runId}:{event.sequence}",
        "createdAt": event.createdAt,
    }

    if event.event.type == "started":
        return {
            **payload,
            "type": "stream_start",
        }

    if event.event.type == "token":
        value = data.get("value")
        if isinstance(value, str):
            return {
                **payload,
                "type": "token",
                "value": value,
            }
        return None

    if event.event.type == "tool_call_start":
        tool_call_id = data.get("toolCallId")
        tool_name = data.get("toolName")
        if isinstance(tool_call_id, str) and isinstance(tool_name, str):
            return {
                **payload,
                "type": "tool_call_start",
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "input": data.get("input"),
            }
        return None

    if event.event.type == "tool_call_end":
        tool_call_id = data.get("toolCallId")
        tool_name = data.get("toolName")
        if isinstance(tool_call_id, str) and isinstance(tool_name, str):
            return {
                **payload,
                "type": "tool_call_end",
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "output": data.get("output"),
            }
        return None

    if event.event.type == "error":
        error = data.get("error")
        return {
            **payload,
            "type": "stream_error",
            "error": error if isinstance(error, str) else "Run failed.",
        }

    if event.event.type == "cancelled":
        reason = data.get("reason")
        return {
            **payload,
            "type": "stream_cancelled",
            "reason": reason if isinstance(reason, str) else "Run stopped by user.",
        }

    if event.event.type == "completed":
        return {
            **payload,
            "type": "stream_end",
        }

    if event.event.type == "subagent_tool_event":
        parent_tool_call_id = data.get("parentToolCallId")
        child_run_id = data.get("childRunId")
        child_event_type = data.get("childEventType")
        tool_call_id = data.get("toolCallId")
        tool_name = data.get("toolName")
        error = data.get("error")

        if (
            not isinstance(parent_tool_call_id, str)
            or not isinstance(child_run_id, str)
            or child_event_type
            not in {
                "stream_start",
                "tool_call_start",
                "tool_call_end",
                "tool_call_error",
                "stream_end",
                "stream_error",
            }
        ):
            return None

        return {
            **payload,
            "type": "subagent_tool_event",
            "parentToolCallId": parent_tool_call_id,
            "childRunId": child_run_id,
            "childEventType": child_event_type,
            "toolCallId": tool_call_id if isinstance(tool_call_id, str) else None,
            "toolName": tool_name if isinstance(tool_name, str) else None,
            "input": data.get("input"),
            "output": data.get("output"),
            "error": error if isinstance(error, str) else None,
        }

    return None
