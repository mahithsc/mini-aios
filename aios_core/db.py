from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path

from .workspace import ensure_workspace_dir

_WORKSPACE_DIR = ensure_workspace_dir()
DB_PATH = str(_WORKSPACE_DIR / "aios.db")
LEGACY_DB_PATH = str(_WORKSPACE_DIR / "crons.db")


def _migrate_legacy_db_if_needed(db_path: str) -> None:
    target = Path(db_path)
    legacy = Path(LEGACY_DB_PATH)

    if target.exists() or not legacy.exists() or target == legacy:
        return

    shutil.copy2(legacy, target)


def get_db_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    _migrate_legacy_db_if_needed(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_app_db(db_path: str = DB_PATH) -> None:
    with get_db_connection(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version       INTEGER PRIMARY KEY,
                name          TEXT NOT NULL,
                checksum      TEXT NOT NULL,
                applied_at    INTEGER NOT NULL,
                app_release   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id              TEXT PRIMARY KEY,
                source          TEXT NOT NULL CHECK (source IN ('chat', 'cron', 'system')),
                source_id       TEXT,
                run_id          TEXT,
                chat_id         TEXT,
                level           TEXT NOT NULL CHECK (level IN ('info', 'success', 'warning', 'error')),
                title           TEXT NOT NULL,
                body            TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL,
                dismissed_at    INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_notifications_active_recent
                ON notifications(dismissed_at, created_at DESC);

            CREATE INDEX IF NOT EXISTS idx_notifications_run_id
                ON notifications(run_id);

            CREATE INDEX IF NOT EXISTS idx_notifications_chat_id
                ON notifications(chat_id);

            CREATE INDEX IF NOT EXISTS idx_notifications_source
                ON notifications(source, source_id);

            CREATE TABLE IF NOT EXISTS cloud_device_events (
                event_id       TEXT PRIMARY KEY,
                sequence       INTEGER NOT NULL,
                event_type     TEXT NOT NULL,
                payload_json   TEXT NOT NULL,
                received_at    INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS gateway_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT NOT NULL,
                type            TEXT NOT NULL,
                payload_json    TEXT NOT NULL,
                source_event_id TEXT,
                created_at      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_gateway_events_session_id_id
                ON gateway_events(session_id, id);
        """)
        gateway_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(gateway_events)").fetchall()
        }
        if "source_event_id" not in gateway_columns:
            conn.execute("ALTER TABLE gateway_events ADD COLUMN source_event_id TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_events_source_event_id "
            "ON gateway_events(source_event_id) WHERE source_event_id IS NOT NULL"
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (
                version, name, checksum, applied_at, app_release
            ) VALUES (1, 'baseline', 'baseline-v1', ?, ?)
            """,
            (
                int(time.time() * 1000),
                os.getenv("AIOS_RELEASE_ID", "development"),
            ),
        )
