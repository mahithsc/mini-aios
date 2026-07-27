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
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def initialize_app_db(db_path: str = DB_PATH) -> None:
    with get_db_connection(db_path) as conn:
        # WAL lets readers continue while a chat/run event is being persisted.
        # SQLite still serializes writers, so chat mutations use short
        # transactions and a busy timeout rather than holding a connection
        # open for the duration of a model response.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS device_identity (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                device_id   TEXT NOT NULL,
                created_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS device_link (
                id               INTEGER PRIMARY KEY CHECK (id = 1),
                device_token     TEXT NOT NULL,
                local_token      TEXT NOT NULL,
                owner_user_id    TEXT NOT NULL,
                owner_email      TEXT,
                slug             TEXT,
                paired_at        INTEGER NOT NULL,
                connector_token  TEXT,
                hostname         TEXT
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

            CREATE TABLE IF NOT EXISTS gateway_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT NOT NULL,
                type            TEXT NOT NULL,
                payload_json    TEXT NOT NULL,
                created_at      TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_gateway_events_session_id_id
                ON gateway_events(session_id, id);

            CREATE TABLE IF NOT EXISTS chats (
                id          TEXT PRIMARY KEY,
                title       TEXT,
                status      TEXT NOT NULL DEFAULT 'idle'
                            CHECK (status IN ('idle', 'streaming', 'error', 'cancelled')),
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_chats_updated_at
                ON chats(updated_at DESC);

            CREATE TABLE IF NOT EXISTS chat_messages (
                id          TEXT PRIMARY KEY,
                chat_id     TEXT NOT NULL,
                position    INTEGER NOT NULL,
                role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content     TEXT,
                run_id      TEXT,
                status      TEXT NOT NULL
                            CHECK (status IN (
                                'pending', 'streaming', 'complete', 'error', 'cancelled'
                            )),
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                UNIQUE (chat_id, position)
            );

            CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_position
                ON chat_messages(chat_id, position);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_run_id
                ON chat_messages(run_id)
                WHERE run_id IS NOT NULL;

            CREATE TABLE IF NOT EXISTS message_attachments (
                id           TEXT PRIMARY KEY,
                message_id   TEXT NOT NULL,
                position     INTEGER NOT NULL,
                kind         TEXT NOT NULL CHECK (kind IN ('image', 'file', 'audio')),
                name         TEXT NOT NULL,
                file_path    TEXT NOT NULL,
                mime_type    TEXT,
                size_bytes   INTEGER,
                content_hash TEXT,
                uploaded_at  INTEGER,
                FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE CASCADE,
                UNIQUE (message_id, position)
            );

            CREATE INDEX IF NOT EXISTS idx_message_attachments_message_position
                ON message_attachments(message_id, position);

            CREATE TABLE IF NOT EXISTS attachment_representations (
                id            TEXT PRIMARY KEY,
                attachment_id TEXT NOT NULL,
                position      INTEGER NOT NULL DEFAULT 0,
                kind          TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'ready'
                              CHECK (status IN ('pending', 'ready', 'error')),
                text_content  TEXT,
                file_path     TEXT,
                mime_type     TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at    INTEGER NOT NULL,
                updated_at    INTEGER NOT NULL,
                FOREIGN KEY (attachment_id)
                    REFERENCES message_attachments(id) ON DELETE CASCADE,
                UNIQUE (attachment_id, kind, position)
            );

            CREATE INDEX IF NOT EXISTS idx_attachment_representations_attachment
                ON attachment_representations(attachment_id, kind, position);

            CREATE TABLE IF NOT EXISTS assistant_events (
                id            TEXT PRIMARY KEY,
                message_id    TEXT NOT NULL,
                sequence      INTEGER NOT NULL,
                type          TEXT NOT NULL,
                payload_json  TEXT NOT NULL DEFAULT '{}',
                created_at    INTEGER NOT NULL,
                FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE CASCADE,
                UNIQUE (message_id, sequence)
            );

            CREATE INDEX IF NOT EXISTS idx_assistant_events_message_sequence
                ON assistant_events(message_id, sequence);

            CREATE TABLE IF NOT EXISTS chat_imports (
                source_path       TEXT PRIMARY KEY,
                imported_at       INTEGER NOT NULL,
                chat_count        INTEGER NOT NULL,
                message_count     INTEGER NOT NULL,
                event_count       INTEGER NOT NULL,
                attachment_count  INTEGER NOT NULL,
                skipped_count     INTEGER NOT NULL
            );
        """)
        # Upgrade older device_link tables (columns added after first release).
        for column in ("connector_token TEXT", "hostname TEXT"):
            try:
                conn.execute(f"ALTER TABLE device_link ADD COLUMN {column}")
            except sqlite3.OperationalError:
                pass  # column already exists

        try:
            conn.execute("ALTER TABLE message_attachments ADD COLUMN content_hash TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists


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
    connector_token: str | None = None,
    hostname: str | None = None,
    db_path: str = DB_PATH,
) -> None:
    """Persist the result of a successful pairing (single-row `device_link`)."""
    initialize_app_db(db_path)
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO device_link
                (id, device_token, local_token, owner_user_id, owner_email, slug,
                 paired_at, connector_token, hostname)
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                device_token = excluded.device_token,
                local_token = excluded.local_token,
                owner_user_id = excluded.owner_user_id,
                owner_email = excluded.owner_email,
                slug = excluded.slug,
                paired_at = excluded.paired_at,
                connector_token = excluded.connector_token,
                hostname = excluded.hostname
            """,
            (
                device_token, local_token, owner_user_id, owner_email, slug,
                paired_at, connector_token, hostname,
            ),
        )


def get_device_link(db_path: str = DB_PATH) -> dict | None:
    """Return the current pairing (owner/tokens/slug/tunnel), or None if unpaired."""
    initialize_app_db(db_path)
    with get_db_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT device_token, local_token, owner_user_id, owner_email, slug,
                   paired_at, connector_token, hostname
            FROM device_link WHERE id = 1
            """
        ).fetchone()
    if row is None:
        return None
    keys = (
        "device_token", "local_token", "owner_user_id", "owner_email", "slug",
        "paired_at", "connector_token", "hostname",
    )
    return dict(zip(keys, row))


def clear_device_link(db_path: str = DB_PATH) -> None:
    initialize_app_db(db_path)
    with get_db_connection(db_path) as conn:
        conn.execute("DELETE FROM device_link WHERE id = 1")
