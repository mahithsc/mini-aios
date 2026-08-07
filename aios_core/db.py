from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

from .workspace import get_runtime_paths

DB_PATH = str(get_runtime_paths().database)
_LEGACY_DB_MIGRATION_READY: set[str] = set()
_LEGACY_DB_MIGRATION_LOCK = threading.Lock()


def _legacy_db_candidates() -> list[Path]:
    paths = get_runtime_paths()
    legacy_archive = paths.state / "legacy_workspace"
    roots = (paths.root, paths.workspace, legacy_archive)
    candidates = [
        *(root / "aios.db" for root in roots),
        *(root / "crons.db" for root in roots),
    ]
    return list(dict.fromkeys(path.expanduser().resolve() for path in candidates))


def _backup_database(source: Path, target: Path) -> None:
    """Create the initial state database without leaving a partial target."""
    fd, temporary_name = tempfile.mkstemp(
        prefix=".aios-db-migrate.",
        suffix=".db",
        dir=str(target.parent),
    )
    os.close(fd)
    temporary = Path(temporary_name)
    temporary.unlink(missing_ok=True)
    try:
        source_uri = f"file:{source.resolve()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=5.0) as source_connection:
            with sqlite3.connect(temporary, timeout=5.0) as destination:
                source_connection.backup(destination)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _ensure_cron_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS crons (
            id                TEXT PRIMARY KEY,
            name              TEXT NOT NULL,
            description       TEXT NOT NULL,
            instructions      TEXT NOT NULL,
            schedule          TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'active',
            created_at        TEXT NOT NULL,
            last_run_at       TEXT,
            schedule_timezone TEXT,
            run_at_utc        TEXT
        );

        CREATE TABLE IF NOT EXISTS cron_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            cron_id     TEXT NOT NULL REFERENCES crons(id),
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            output      TEXT,
            status      TEXT NOT NULL
        );
        """
    )
    cron_columns = _table_columns(connection, "crons")
    if "schedule_timezone" not in cron_columns:
        connection.execute("ALTER TABLE crons ADD COLUMN schedule_timezone TEXT")
    if "run_at_utc" not in cron_columns:
        connection.execute("ALTER TABLE crons ADD COLUMN run_at_utc TEXT")


def _source_rows(
    connection: sqlite3.Connection,
    table: str,
) -> list[dict[str, object]]:
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    if not columns:
        return []
    connection.row_factory = sqlite3.Row
    return [
        dict(row)
        for row in connection.execute(f"SELECT * FROM {table}")
    ]


def _merge_legacy_crons(
    source_path: Path,
    destination: sqlite3.Connection,
) -> None:
    source_uri = f"file:{source_path.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=5.0) as source:
        source_tables = {
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "crons" not in source_tables:
            return

        _ensure_cron_tables(destination)
        default_timezone = os.getenv(
            "AIOS_DEFAULT_TIMEZONE",
            "America/New_York",
        )
        for cron in _source_rows(source, "crons"):
            cron_id = cron.get("id")
            if not isinstance(cron_id, str) or not cron_id:
                continue
            schedule = str(cron.get("schedule") or "")
            schedule_timezone = cron.get("schedule_timezone")
            if not schedule_timezone and schedule:
                schedule_timezone = default_timezone
            destination.execute(
                """
                INSERT INTO crons (
                    id, name, description, instructions, schedule, status,
                    created_at, last_run_at, schedule_timezone, run_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    cron_id,
                    str(cron.get("name") or ""),
                    str(cron.get("description") or ""),
                    str(cron.get("instructions") or ""),
                    schedule,
                    str(cron.get("status") or "active"),
                    str(cron.get("created_at") or ""),
                    cron.get("last_run_at"),
                    schedule_timezone,
                    cron.get("run_at_utc"),
                ),
            )

        if "cron_runs" not in source_tables:
            return

        known_crons = {
            row[0] for row in destination.execute("SELECT id FROM crons")
        }
        existing_runs = {
            tuple(row)
            for row in destination.execute(
                """
                SELECT cron_id, started_at, finished_at, output, status
                FROM cron_runs
                """
            )
        }
        for run in _source_rows(source, "cron_runs"):
            values = (
                run.get("cron_id"),
                run.get("started_at"),
                run.get("finished_at"),
                run.get("output"),
                run.get("status"),
            )
            if values[0] not in known_crons or values in existing_runs:
                continue
            destination.execute(
                """
                INSERT INTO cron_runs (
                    cron_id, started_at, finished_at, output, status
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                values,
            )
            existing_runs.add(values)


def _migrate_legacy_db_if_needed(db_path: str) -> None:
    target = Path(db_path).expanduser().resolve()
    if target != Path(DB_PATH).expanduser().resolve():
        return

    target_key = str(target)
    if target_key in _LEGACY_DB_MIGRATION_READY:
        return

    with _LEGACY_DB_MIGRATION_LOCK:
        if target_key in _LEGACY_DB_MIGRATION_READY:
            return

        candidates = [
            candidate
            for candidate in _legacy_db_candidates()
            if candidate.exists() and candidate != target
        ]
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() and candidates:
            _backup_database(candidates[0], target)

        if target.exists():
            with sqlite3.connect(target, timeout=5.0) as destination:
                destination.execute(
                    """
                    CREATE TABLE IF NOT EXISTS legacy_database_imports (
                        source_path TEXT PRIMARY KEY,
                        imported_at INTEGER NOT NULL
                    )
                    """
                )
                imported_sources = {
                    row[0]
                    for row in destination.execute(
                        "SELECT source_path FROM legacy_database_imports"
                    )
                }
                for source in candidates:
                    source_key = str(source)
                    if source_key in imported_sources:
                        continue
                    _merge_legacy_crons(source, destination)
                    destination.execute(
                        """
                        INSERT INTO legacy_database_imports (
                            source_path, imported_at
                        )
                        VALUES (?, ?)
                        """,
                        (source_key, int(time.time() * 1000)),
                    )

        _LEGACY_DB_MIGRATION_READY.add(target_key)


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
