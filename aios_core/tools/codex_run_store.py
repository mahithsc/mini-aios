from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ..db import DB_PATH


_SCHEMA = """
CREATE TABLE IF NOT EXISTS codex_runs (
    id                  TEXT PRIMARY KEY,
    session_id          TEXT,
    parent_run_id       TEXT,
    parent_tool_call_id TEXT,
    status              TEXT NOT NULL,
    task                TEXT NOT NULL,
    workdir             TEXT NOT NULL,
    model               TEXT,
    capabilities_json   TEXT NOT NULL DEFAULT '[]',
    thread_id           TEXT,
    turn_id             TEXT,
    pending_input_json  TEXT,
    result              TEXT,
    error               TEXT,
    process_pid         INTEGER,
    process_identity    TEXT,
    recovery_count      INTEGER NOT NULL DEFAULT 0,
    verification_status TEXT,
    deploy_state_json   TEXT NOT NULL DEFAULT '{}',
    contract_version    INTEGER NOT NULL DEFAULT 1,
    deployment_requested INTEGER NOT NULL DEFAULT 0,
    app_state_json      TEXT NOT NULL DEFAULT '{}',
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    finished_at         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_codex_runs_session_updated
    ON codex_runs(session_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_codex_runs_status
    ON codex_runs(status);

CREATE TABLE IF NOT EXISTS codex_run_events (
    job_id              TEXT NOT NULL,
    sequence            INTEGER NOT NULL,
    event_json          TEXT NOT NULL,
    channel             TEXT NOT NULL DEFAULT 'poll',
    session_id          TEXT,
    event_type          TEXT,
    delivered_at        INTEGER,
    created_at          INTEGER NOT NULL,
    PRIMARY KEY (job_id, sequence),
    FOREIGN KEY (job_id) REFERENCES codex_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS codex_run_signals (
    job_id              TEXT NOT NULL,
    signal              TEXT NOT NULL,
    created_at          INTEGER NOT NULL,
    delivered_at        INTEGER,
    continuation_run_id TEXT,
    delivery_attempts   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (job_id, signal),
    FOREIGN KEY (job_id) REFERENCES codex_runs(id) ON DELETE CASCADE
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decode_json(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


class CodexRunStore:
    """Durable lifecycle state for Codex child runs.

    File-backed stores open short-lived connections. The in-memory mode is used
    by unit-test ``CodexJobManager`` instances and keeps one thread-safe
    connection alive for the lifetime of the store.
    """

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._memory_connection: sqlite3.Connection | None = None
        if db_path == ":memory:":
            self._memory_connection = sqlite3.connect(
                ":memory:", check_same_thread=False
            )
            self._memory_connection.row_factory = sqlite3.Row
            self._memory_connection.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self._memory_connection is not None:
            return self._memory_connection
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.executescript(_SCHEMA)
                self._ensure_columns(
                    connection,
                    "codex_runs",
                    {
                        "process_pid": "INTEGER",
                        "process_identity": "TEXT",
                        "recovery_count": "INTEGER NOT NULL DEFAULT 0",
                        "verification_status": "TEXT",
                        "deploy_state_json": "TEXT NOT NULL DEFAULT '{}'",
                        "contract_version": "INTEGER NOT NULL DEFAULT 1",
                        "deployment_requested": "INTEGER NOT NULL DEFAULT 0",
                        "app_state_json": "TEXT NOT NULL DEFAULT '{}'",
                    },
                )
                self._ensure_columns(
                    connection,
                    "codex_run_events",
                    {
                        "channel": "TEXT NOT NULL DEFAULT 'poll'",
                        "session_id": "TEXT",
                        "event_type": "TEXT",
                        "delivered_at": "INTEGER",
                    },
                )
                self._ensure_columns(
                    connection,
                    "codex_run_signals",
                    {
                        "continuation_run_id": "TEXT",
                        "delivery_attempts": "INTEGER NOT NULL DEFAULT 0",
                    },
                )
                # A process may have crashed after claiming a signal but before
                # submitting its continuation. Make it eligible for redelivery.
                connection.execute(
                    "UPDATE codex_run_signals SET delivered_at = NULL "
                    "WHERE delivered_at = -1"
                )
                connection.commit()
            finally:
                if self._memory_connection is None:
                    connection.close()

    @staticmethod
    def _ensure_columns(
        connection: sqlite3.Connection,
        table: str,
        columns: dict[str, str],
    ) -> None:
        existing = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, declaration in columns.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                )

    def create(
        self,
        *,
        job_id: str,
        session_id: str | None,
        parent_run_id: str | None,
        parent_tool_call_id: str | None,
        task: str,
        workdir: str,
        model: str | None,
        capabilities: list[str],
        contract_version: int = 1,
        deployment_requested: bool = False,
        app_state: dict[str, Any] | None = None,
    ) -> None:
        now = _now_ms()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO codex_runs (
                    id, session_id, parent_run_id, parent_tool_call_id,
                    status, task, workdir, model, capabilities_json,
                    contract_version, deployment_requested, app_state_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    session_id,
                    parent_run_id,
                    parent_tool_call_id,
                    task,
                    workdir,
                    model,
                    json.dumps(capabilities),
                    int(contract_version),
                    int(deployment_requested),
                    json.dumps(app_state or {}, default=str, sort_keys=True),
                    now,
                    now,
                ),
            )

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        pending_input: dict[str, Any] | None = None,
        clear_pending_input: bool = False,
        result: str | None = None,
        error: str | None = None,
        terminal: bool = False,
        process_pid: int | None = None,
        process_identity: str | None = None,
        clear_process: bool = False,
        recovery_count: int | None = None,
        verification_status: str | None = None,
        deploy_state: dict[str, Any] | None = None,
        app_state: dict[str, Any] | None = None,
    ) -> None:
        assignments = ["updated_at = ?"]
        values: list[Any] = [_now_ms()]
        for column, value in (
            ("status", status),
            ("thread_id", thread_id),
            ("turn_id", turn_id),
            ("result", result),
            ("error", error),
            ("process_pid", process_pid),
            ("process_identity", process_identity),
            ("recovery_count", recovery_count),
            ("verification_status", verification_status),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if clear_process:
            assignments.extend(["process_pid = NULL", "process_identity = NULL"])
        if pending_input is not None:
            assignments.append("pending_input_json = ?")
            values.append(json.dumps(pending_input, default=str))
        elif clear_pending_input:
            assignments.append("pending_input_json = NULL")
        if deploy_state is not None:
            assignments.append("deploy_state_json = ?")
            values.append(json.dumps(deploy_state, default=str, sort_keys=True))
        if app_state is not None:
            assignments.append("app_state_json = ?")
            values.append(json.dumps(app_state, default=str, sort_keys=True))
        if terminal:
            assignments.append("finished_at = ?")
            values.append(_now_ms())
        values.append(job_id)
        with self._transaction() as connection:
            connection.execute(
                f"UPDATE codex_runs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def append_event(
        self,
        job_id: str,
        event: dict[str, Any],
        *,
        channel: str = "poll",
        session_id: str | None = None,
        event_type: str | None = None,
    ) -> int:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM codex_run_events "
                "WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            sequence = int(row[0])
            connection.execute(
                "INSERT INTO codex_run_events "
                "(job_id, sequence, event_json, channel, session_id, event_type, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    sequence,
                    json.dumps(event, default=str),
                    channel,
                    session_id,
                    event_type,
                    _now_ms(),
                ),
            )
            return sequence

    def append_gateway_event(
        self,
        job_id: str,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> int:
        return self.append_event(
            job_id,
            payload,
            channel="gateway",
            session_id=session_id,
            event_type=event_type,
        )

    def events_after(
        self, job_id: str, cursor: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        with self._query() as connection:
            rows = connection.execute(
                "SELECT sequence, event_json FROM codex_run_events "
                "WHERE job_id = ? AND channel = 'poll' AND sequence > ? "
                "ORDER BY sequence",
                (job_id, max(0, int(cursor))),
            ).fetchall()
        events = [_decode_json(row["event_json"], {}) for row in rows]
        next_cursor = int(rows[-1]["sequence"]) if rows else max(0, int(cursor))
        return events, next_cursor

    def pending_gateway_events(self) -> list[dict[str, Any]]:
        with self._query() as connection:
            rows = connection.execute(
                "SELECT job_id, sequence, session_id, event_type, event_json "
                "FROM codex_run_events WHERE channel = 'gateway' "
                "AND delivered_at IS NULL ORDER BY created_at, sequence"
            ).fetchall()
        return [
            {
                "job_id": str(row["job_id"]),
                "sequence": int(row["sequence"]),
                "session_id": str(row["session_id"] or ""),
                "event_type": str(row["event_type"] or "codex.progress"),
                "payload": {
                    **_decode_json(row["event_json"], {}),
                    "codex_event_id": f"{row['job_id']}:{row['sequence']}",
                },
            }
            for row in rows
        ]

    def complete_gateway_event(self, job_id: str, sequence: int) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE codex_run_events SET delivered_at = ? "
                "WHERE job_id = ? AND sequence = ? AND channel = 'gateway'",
                (_now_ms(), job_id, sequence),
            )

    def enqueue_signal(self, job_id: str, signal: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO codex_run_signals "
                "(job_id, signal, created_at) VALUES (?, ?, ?)",
                (job_id, signal, _now_ms()),
            )

    def claim_signal(self, job_id: str, signal: str) -> bool:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE codex_run_signals SET delivered_at = -1, "
                "delivery_attempts = delivery_attempts + 1 "
                "WHERE job_id = ? AND signal = ? AND delivered_at IS NULL",
                (job_id, signal),
            )
            return cursor.rowcount == 1

    def complete_signal(
        self,
        job_id: str,
        signal: str,
        continuation_run_id: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE codex_run_signals SET delivered_at = ?, "
                "continuation_run_id = COALESCE(?, continuation_run_id) "
                "WHERE job_id = ? AND signal = ?",
                (_now_ms(), continuation_run_id, job_id, signal),
            )

    def release_signal(self, job_id: str, signal: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE codex_run_signals SET delivered_at = NULL "
                "WHERE job_id = ? AND signal = ? AND delivered_at = -1",
                (job_id, signal),
            )

    def pending_signals(self) -> list[tuple[str, str]]:
        with self._query() as connection:
            rows = connection.execute(
                "SELECT job_id, signal FROM codex_run_signals "
                "WHERE delivered_at IS NULL ORDER BY created_at"
            ).fetchall()
        return [(str(row["job_id"]), str(row["signal"])) for row in rows]

    def signal(self, job_id: str, signal: str) -> dict[str, Any] | None:
        with self._query() as connection:
            row = connection.execute(
                "SELECT * FROM codex_run_signals WHERE job_id = ? AND signal = ?",
                (job_id, signal),
            ).fetchone()
        return dict(row) if row is not None else None

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._query() as connection:
            row = connection.execute(
                "SELECT * FROM codex_runs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._shape(row) if row is not None else None

    def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        with self._query() as connection:
            rows = connection.execute(
                "SELECT * FROM codex_runs WHERE session_id = ? "
                "ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        return [self._shape(row) for row in rows]

    def active(self) -> list[dict[str, Any]]:
        with self._query() as connection:
            rows = connection.execute(
                "SELECT * FROM codex_runs "
                "WHERE status IN ('running', 'awaiting_input') "
                "ORDER BY created_at"
            ).fetchall()
        return [self._shape(row) for row in rows]

    def cleanup(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = _now_ms() - retention_days * 86_400_000
        with self._transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM codex_runs WHERE finished_at IS NOT NULL "
                "AND finished_at < ?",
                (cutoff,),
            )
            return cursor.rowcount

    def metrics(self) -> dict[str, Any]:
        now = _now_ms()
        with self._query() as connection:
            statuses = connection.execute(
                "SELECT status, COUNT(*) AS count FROM codex_runs GROUP BY status"
            ).fetchall()
            counts = connection.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM codex_run_events) AS events, "
                "(SELECT COUNT(*) FROM codex_run_events WHERE channel = 'gateway' "
                " AND delivered_at IS NULL) AS gateway_pending, "
                "(SELECT COUNT(*) FROM codex_run_signals "
                " WHERE delivered_at IS NULL OR delivered_at = -1) AS signals_pending, "
                "(SELECT COALESCE(SUM(recovery_count), 0) FROM codex_runs) AS recoveries"
            ).fetchone()
            oldest = connection.execute(
                "SELECT MIN(created_at) FROM codex_runs "
                "WHERE status IN ('running', 'awaiting_input')"
            ).fetchone()[0]
        return {
            "status_counts": {
                str(row["status"]): int(row["count"]) for row in statuses
            },
            "events": int(counts["events"]),
            "gateway_events_pending": int(counts["gateway_pending"]),
            "continuation_signals_pending": int(counts["signals_pending"]),
            "recoveries": int(counts["recoveries"]),
            "oldest_active_age_ms": max(0, now - int(oldest)) if oldest else None,
        }

    def _shape(self, row: sqlite3.Row) -> dict[str, Any]:
        decoded_app_state = _decode_json(row["app_state_json"], {})
        app_state = decoded_app_state if isinstance(decoded_app_state, dict) else {}
        return {
            "job_id": row["id"],
            "session_id": row["session_id"],
            "parent_run_id": row["parent_run_id"],
            "parent_tool_call_id": row["parent_tool_call_id"],
            "status": row["status"],
            "task": row["task"],
            "workdir": row["workdir"],
            "model": row["model"],
            "capabilities": _decode_json(row["capabilities_json"], []),
            "thread_id": row["thread_id"],
            "turn_id": row["turn_id"],
            "pending_input": _decode_json(row["pending_input_json"], None),
            "result": row["result"],
            "error": row["error"],
            "process_pid": row["process_pid"],
            "process_identity": row["process_identity"],
            "recovery_count": row["recovery_count"],
            "verification_status": row["verification_status"],
            "deploy_state": _decode_json(row["deploy_state_json"], {}),
            "contract_version": int(row["contract_version"] or 1),
            "deployment_requested": bool(row["deployment_requested"]),
            "app_state": app_state,
            "workspace_handoff": app_state.get("workspace_handoff"),
            "display_status": self._display_status(row),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }

    @staticmethod
    def _display_status(row: sqlite3.Row) -> str:
        status = str(row["status"])
        verification = row["verification_status"]
        if status == "awaiting_input":
            return "awaiting_input"
        if status == "running":
            return "recovering" if int(row["recovery_count"] or 0) else "working"
        if status == "done" and verification in {"queued", "running"}:
            return "verifying_changes"
        if status == "done" and verification == "error":
            return "verification_failed"
        if status == "done" and verification == "cancelled":
            return "verification_cancelled"
        if status == "done":
            return "completed"
        return status

    class _ConnectionContext:
        def __init__(self, store: "CodexRunStore", transaction: bool) -> None:
            self.store = store
            self.transaction = transaction
            self.connection: sqlite3.Connection | None = None

        def __enter__(self) -> sqlite3.Connection:
            self.store._lock.acquire()
            self.connection = self.store._connect()
            return self.connection

        def __exit__(self, exc_type, exc, traceback) -> None:
            assert self.connection is not None
            try:
                if self.transaction:
                    if exc_type is None:
                        self.connection.commit()
                    else:
                        self.connection.rollback()
            finally:
                if self.store._memory_connection is None:
                    self.connection.close()
                self.store._lock.release()

    def _transaction(self) -> "CodexRunStore._ConnectionContext":
        return self._ConnectionContext(self, transaction=True)

    def _query(self) -> "CodexRunStore._ConnectionContext":
        return self._ConnectionContext(self, transaction=False)
