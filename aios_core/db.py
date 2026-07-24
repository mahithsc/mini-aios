from __future__ import annotations

import shutil
import sqlite3
import time
import uuid
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
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS device_identity (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                device_id   TEXT NOT NULL,
                created_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS device_link (
                id             INTEGER PRIMARY KEY CHECK (id = 1),
                device_token   TEXT NOT NULL,
                local_token    TEXT NOT NULL,
                owner_user_id  TEXT NOT NULL,
                owner_email    TEXT,
                slug           TEXT,
                paired_at      INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id              TEXT PRIMARY KEY,
                source          TEXT NOT NULL CHECK (source IN ('chat', 'cron', 'heartbeat', 'system')),
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
        """)


def get_or_create_device_id(db_path: str = DB_PATH) -> str:
    """Return this box's stable device id, generating and persisting one on
    first call.

    The id lives in the single-row `device_identity` table so it survives
    reboots and identifies this physical box across the account-pairing flow.
    """
    initialize_app_db(db_path)
    with get_db_connection(db_path) as conn:
        row = conn.execute("SELECT device_id FROM device_identity WHERE id = 1").fetchone()
        if row is not None:
            return row[0]

        device_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO device_identity (id, device_id, created_at) VALUES (1, ?, ?)",
            (device_id, int(time.time())),
        )
        return device_id


def save_device_link(
    *,
    device_token: str,
    local_token: str,
    owner_user_id: str,
    owner_email: str | None,
    slug: str | None,
    paired_at: int,
    db_path: str = DB_PATH,
) -> None:
    """Persist the result of a successful pairing (single-row `device_link`)."""
    initialize_app_db(db_path)
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO device_link
                (id, device_token, local_token, owner_user_id, owner_email, slug, paired_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                device_token = excluded.device_token,
                local_token = excluded.local_token,
                owner_user_id = excluded.owner_user_id,
                owner_email = excluded.owner_email,
                slug = excluded.slug,
                paired_at = excluded.paired_at
            """,
            (device_token, local_token, owner_user_id, owner_email, slug, paired_at),
        )


def get_device_link(db_path: str = DB_PATH) -> dict | None:
    """Return the current pairing (owner/tokens/slug), or None if unpaired."""
    initialize_app_db(db_path)
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT device_token, local_token, owner_user_id, owner_email, slug, paired_at
            FROM device_link WHERE id = 1
            """
        ).fetchone()
    if row is None:
        return None
    keys = ("device_token", "local_token", "owner_user_id", "owner_email", "slug", "paired_at")
    return dict(zip(keys, row))


def clear_device_link(db_path: str = DB_PATH) -> None:
    initialize_app_db(db_path)
    with get_db_connection(db_path) as conn:
        conn.execute("DELETE FROM device_link WHERE id = 1")
