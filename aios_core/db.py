from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from .release import DATABASE_SCHEMA_VERSION
from .workspace import ensure_data_dir, get_legacy_state_db_path, get_state_dir

ensure_data_dir()
DB_PATH = str(get_state_dir() / "aios.db")
LEGACY_DB_PATH = str(get_state_dir() / "crons.db")
_EXPECTED_SCHEMA_MIGRATIONS = {
    1: ("baseline", "baseline-v1"),
    2: ("chat_sqlite_storage", "chat-sqlite-v1"),
    3: ("canonical_conversation_storage", "conversation-v1"),
    4: ("conversation_rail_metadata", "conversation-v2"),
    5: ("cloud_deployment_runtime", "cloud-deploy-v1"),
}
log = logging.getLogger(__name__)

if max(_EXPECTED_SCHEMA_MIGRATIONS) != DATABASE_SCHEMA_VERSION:
    raise RuntimeError(
        "Database migration registry does not match release schema version"
    )


def _migrate_legacy_db_if_needed(db_path: str) -> None:
    target = Path(db_path)
    legacy = Path(LEGACY_DB_PATH)

    if target.exists() or not legacy.exists() or target == legacy:
        return

    shutil.copy2(legacy, target)


def get_db_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    _migrate_legacy_db_if_needed(db_path)
    Path(db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5.0, uri=True)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _import_legacy_device_link(connection: sqlite3.Connection, db_path: str) -> None:
    """Restore pairing only when the canonical database has no pairing."""

    if Path(db_path).expanduser().resolve() != Path(DB_PATH).expanduser().resolve():
        return
    if connection.execute("SELECT 1 FROM device_link WHERE id = 1").fetchone():
        return
    source_path = get_legacy_state_db_path()
    if not source_path.is_file() or source_path.resolve() == Path(db_path).resolve():
        return

    source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source:
            table_exists = source.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'device_link'"
            ).fetchone()
            if table_exists is None:
                return
            row = source.execute(
                "SELECT id, device_token FROM device_link WHERE id = 1"
            ).fetchone()
        if row is not None:
            connection.execute(
                "INSERT OR IGNORE INTO device_link (id, device_token) VALUES (?, ?)",
                row,
            )
    except sqlite3.Error as exc:
        log.warning("Could not import pairing from legacy state database: %s", exc)


def validate_app_db_schema(
    connection: sqlite3.Connection, *, require_current: bool = True
) -> None:
    """Reject unknown, future, missing, or checksum-mismatched migrations."""

    table_exists = connection.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'schema_migrations'
        """
    ).fetchone()
    if table_exists is None:
        if require_current:
            raise RuntimeError("Database schema migration table is missing")
        return

    rows = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    present: set[int] = set()
    for raw_version, raw_name, raw_checksum in rows:
        version = int(raw_version)
        expected = _EXPECTED_SCHEMA_MIGRATIONS.get(version)
        if expected is None:
            if version > DATABASE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema {version} is newer than supported schema "
                    f"{DATABASE_SCHEMA_VERSION}"
                )
            raise RuntimeError(f"Database contains unknown migration version {version}")
        actual = (str(raw_name), str(raw_checksum))
        if actual != expected:
            raise RuntimeError(
                f"Database migration {version} does not match the expected name/checksum"
            )
        present.add(version)

    if require_current:
        missing = sorted(set(_EXPECTED_SCHEMA_MIGRATIONS).difference(present))
        if missing:
            raise RuntimeError(
                "Database is missing required migration(s): "
                + ", ".join(str(version) for version in missing)
            )


def initialize_app_db(db_path: str = DB_PATH) -> None:
    with get_db_connection(db_path) as conn:
        validate_app_db_schema(conn, require_current=False)
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

            -- Device pairing owns this singleton credential. Cloud deployment
            -- and event delivery read it without coupling those services to
            -- the pairing transport.
            CREATE TABLE IF NOT EXISTS device_link (
                id              INTEGER PRIMARY KEY CHECK (id = 1),
                device_token    TEXT NOT NULL
            );

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

            -- Canonical agent history.  UI chat rows/events remain a read
            -- model for the desktop; these rows are the lossless source used
            -- to rebuild provider-specific conversation input.
            CREATE TABLE IF NOT EXISTS conversation_threads (
                chat_id         TEXT PRIMARY KEY,
                format_version  INTEGER NOT NULL DEFAULT 1,
                default_rail    TEXT NOT NULL DEFAULT 'openai_responses',
                seed_kind       TEXT NOT NULL DEFAULT 'native'
                                CHECK (seed_kind IN ('native', 'legacy_lossy')),
                seeded_at       INTEGER,
                next_item_position INTEGER NOT NULL DEFAULT 0,
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS conversation_turns (
                turn_id         TEXT PRIMARY KEY,
                chat_id         TEXT NOT NULL,
                user_message_id TEXT,
                run_id          TEXT UNIQUE,
                status          TEXT NOT NULL DEFAULT 'queued'
                                CHECK (status IN (
                                    'queued', 'running', 'complete',
                                    'error', 'cancelled'
                                )),
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                UNIQUE (chat_id, user_message_id)
            );

            CREATE INDEX IF NOT EXISTS idx_conversation_turns_chat
                ON conversation_turns(chat_id, created_at, turn_id);

            CREATE TABLE IF NOT EXISTS conversation_items (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id           TEXT NOT NULL,
                scope_key         TEXT NOT NULL DEFAULT 'main',
                rail              TEXT NOT NULL DEFAULT 'openai_responses',
                run_id            TEXT,
                turn_id           TEXT REFERENCES conversation_turns(turn_id)
                                  ON DELETE SET NULL,
                source_message_id TEXT,
                response_id       TEXT,
                response_index    INTEGER,
                position          INTEGER NOT NULL,
                item_type         TEXT NOT NULL,
                role              TEXT,
                call_id           TEXT,
                tool_name         TEXT,
                content_text      TEXT,
                item_json         TEXT NOT NULL,
                dedupe_key        TEXT,
                source            TEXT NOT NULL,
                replayable        INTEGER NOT NULL DEFAULT 1,
                active            INTEGER NOT NULL DEFAULT 1,
                created_at        INTEGER NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_conversation_items_chat_order
                ON conversation_items(chat_id, scope_key, position);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_items_position
                ON conversation_items(chat_id, scope_key, position);

            CREATE INDEX IF NOT EXISTS idx_conversation_items_call
                ON conversation_items(chat_id, scope_key, call_id);

            CREATE INDEX IF NOT EXISTS idx_conversation_items_source_message
                ON conversation_items(chat_id, source_message_id);

            CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_items_dedupe
                ON conversation_items(chat_id, scope_key, dedupe_key)
                WHERE dedupe_key IS NOT NULL;

            -- Raw SDK/application events, including streaming text,
            -- reasoning, function-argument deltas, and nested-agent events.
            -- These are intentionally separate from replayable items because
            -- deltas are observability data, not valid Responses input items.
            CREATE TABLE IF NOT EXISTS conversation_events (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id        TEXT NOT NULL,
                run_id         TEXT NOT NULL,
                turn_id        TEXT REFERENCES conversation_turns(turn_id)
                               ON DELETE SET NULL,
                scope_key      TEXT NOT NULL DEFAULT 'main',
                rail           TEXT NOT NULL DEFAULT 'openai_responses',
                sequence       INTEGER NOT NULL,
                event_type     TEXT NOT NULL,
                item_type      TEXT,
                call_id        TEXT,
                provider_item_id TEXT,
                output_index   INTEGER,
                content_index  INTEGER,
                provider_sequence INTEGER,
                payload_json   TEXT NOT NULL,
                created_at     INTEGER NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                UNIQUE (run_id, scope_key, sequence)
            );

            CREATE INDEX IF NOT EXISTS idx_conversation_events_chat_order
                ON conversation_events(chat_id, scope_key, id);

            CREATE INDEX IF NOT EXISTS idx_conversation_events_call
                ON conversation_events(chat_id, scope_key, call_id);

            -- Side-effect journal.  A call left in `running` after process
            -- loss is `unknown`, not safe to execute again blindly.
            CREATE TABLE IF NOT EXISTS tool_executions (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id          TEXT NOT NULL,
                scope_key        TEXT NOT NULL DEFAULT 'main',
                run_id           TEXT NOT NULL,
                turn_id          TEXT REFERENCES conversation_turns(turn_id)
                                 ON DELETE SET NULL,
                response_id      TEXT,
                response_index   INTEGER,
                output_position  INTEGER,
                call_id          TEXT NOT NULL,
                tool_name        TEXT NOT NULL,
                arguments_json   TEXT NOT NULL,
                status           TEXT NOT NULL
                                 CHECK (status IN (
                                     'pending', 'running', 'completed',
                                     'error', 'cancelled', 'unknown'
                                 )),
                result_json      TEXT,
                error            TEXT,
                created_at       INTEGER NOT NULL,
                started_at       INTEGER,
                completed_at     INTEGER,
                updated_at       INTEGER NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                UNIQUE (chat_id, scope_key, call_id)
            );

            CREATE INDEX IF NOT EXISTS idx_tool_executions_run
                ON tool_executions(run_id, scope_key, id);

            -- Replacing an imported/compatibility transcript deletes its
            -- conversation_threads marker. Keep every canonical child table
            -- in the same invalidation boundary even though they also point
            -- at chats for normal whole-chat cascade deletion.
            CREATE TRIGGER IF NOT EXISTS trg_conversation_threads_delete_history
            AFTER DELETE ON conversation_threads
            BEGIN
                DELETE FROM tool_executions WHERE chat_id = OLD.chat_id;
                DELETE FROM conversation_events WHERE chat_id = OLD.chat_id;
                DELETE FROM conversation_items WHERE chat_id = OLD.chat_id;
                DELETE FROM conversation_turns WHERE chat_id = OLD.chat_id;
            END;
        """)
        attachment_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(message_attachments)")
        }
        if "content_hash" not in attachment_columns:
            conn.execute("ALTER TABLE message_attachments ADD COLUMN content_hash TEXT")

        thread_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversation_threads)")
        }
        if "default_rail" not in thread_columns:
            conn.execute(
                "ALTER TABLE conversation_threads "
                "ADD COLUMN default_rail TEXT NOT NULL DEFAULT 'openai_responses'"
            )

        item_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversation_items)")
        }
        if "rail" not in item_columns:
            conn.execute(
                "ALTER TABLE conversation_items "
                "ADD COLUMN rail TEXT NOT NULL DEFAULT 'openai_responses'"
            )

        event_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(conversation_events)")
        }
        event_column_definitions = {
            "rail": "TEXT NOT NULL DEFAULT 'openai_responses'",
            "provider_item_id": "TEXT",
            "output_index": "INTEGER",
            "content_index": "INTEGER",
            "provider_sequence": "INTEGER",
        }
        for column, definition in event_column_definitions.items():
            if column not in event_columns:
                conn.execute(
                    f"ALTER TABLE conversation_events ADD COLUMN {column} {definition}"
                )

        # Rail is part of provider-item identity. Rebuilding this index is
        # metadata-only and keeps future adapters from colliding with OpenAI
        # item IDs while all rails continue sharing one database and order.
        conn.execute("DROP INDEX IF EXISTS idx_conversation_items_dedupe")
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_items_dedupe
            ON conversation_items(chat_id, scope_key, rail, dedupe_key)
            WHERE dedupe_key IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversation_events_provider_item
            ON conversation_events(chat_id, scope_key, rail, provider_item_id, output_index)
            """
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
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (
                version, name, checksum, applied_at, app_release
            ) VALUES (2, 'chat_sqlite_storage', 'chat-sqlite-v1', ?, ?)
            """,
            (
                int(time.time() * 1000),
                os.getenv("AIOS_RELEASE_ID", "development"),
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (
                version, name, checksum, applied_at, app_release
            ) VALUES (3, 'canonical_conversation_storage', 'conversation-v1', ?, ?)
            """,
            (
                int(time.time() * 1000),
                os.getenv("AIOS_RELEASE_ID", "development"),
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (
                version, name, checksum, applied_at, app_release
            ) VALUES (4, 'conversation_rail_metadata', 'conversation-v2', ?, ?)
            """,
            (
                int(time.time() * 1000),
                os.getenv("AIOS_RELEASE_ID", "development"),
            ),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO schema_migrations (
                version, name, checksum, applied_at, app_release
            ) VALUES (5, 'cloud_deployment_runtime', 'cloud-deploy-v1', ?, ?)
            """,
            (
                int(time.time() * 1000),
                os.getenv("AIOS_RELEASE_ID", "development"),
            ),
        )
        _import_legacy_device_link(conn, db_path)
        validate_app_db_schema(conn)
