from __future__ import annotations

import json
import sqlite3
from typing import Any


def ensure_chat_search_schema(
    connection: sqlite3.Connection,
) -> tuple[bool, bool]:
    """Create search storage and return ``(fts_available, fts_created)``."""
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS chat_search_documents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id  TEXT NOT NULL UNIQUE,
            chat_id     TEXT NOT NULL,
            role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content     TEXT NOT NULL,
            created_at  INTEGER NOT NULL,
            FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE CASCADE,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_chat_search_documents_chat_created
            ON chat_search_documents(chat_id, created_at);
        """
    )

    fts_existed = fts_table_available(connection)
    try:
        connection.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chat_search_fts USING fts5(
                content,
                content='chat_search_documents',
                content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            );

            CREATE TRIGGER IF NOT EXISTS chat_search_documents_ai
            AFTER INSERT ON chat_search_documents BEGIN
                INSERT INTO chat_search_fts(rowid, content)
                VALUES (new.id, new.content);
            END;

            CREATE TRIGGER IF NOT EXISTS chat_search_documents_ad
            AFTER DELETE ON chat_search_documents BEGIN
                INSERT INTO chat_search_fts(chat_search_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END;

            CREATE TRIGGER IF NOT EXISTS chat_search_documents_au
            AFTER UPDATE ON chat_search_documents BEGIN
                INSERT INTO chat_search_fts(chat_search_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
                INSERT INTO chat_search_fts(rowid, content)
                VALUES (new.id, new.content);
            END;
            """
        )
    except sqlite3.OperationalError as exc:
        if "fts5" not in str(exc).lower():
            raise
        return False, False
    return True, not fts_existed


def rebuild_fts_index(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO chat_search_fts(chat_search_fts) VALUES ('rebuild')"
    )


def upsert_search_document(
    connection: sqlite3.Connection,
    *,
    message_id: str,
    chat_id: str,
    role: str,
    content: str,
    created_at: int,
) -> None:
    connection.execute(
        """
        INSERT INTO chat_search_documents
            (message_id, chat_id, role, content, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            chat_id = excluded.chat_id,
            role = excluded.role,
            content = excluded.content,
            created_at = excluded.created_at
        """,
        (message_id, chat_id, role, content, created_at),
    )


def _event_text(payload_json: str) -> str:
    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    value = payload.get("value")
    return value if isinstance(value, str) else ""


def refresh_assistant_search_document(
    connection: sqlite3.Connection,
    message_id: str,
) -> None:
    message_row = connection.execute(
        """
        SELECT chat_id, created_at
        FROM chat_messages
        WHERE id = ? AND role = 'assistant'
        """,
        (message_id,),
    ).fetchone()
    if message_row is None:
        return

    event_rows = connection.execute(
        """
        SELECT payload_json
        FROM assistant_events
        WHERE message_id = ? AND type = 'token'
        ORDER BY sequence ASC
        """,
        (message_id,),
    ).fetchall()
    content = "".join(_event_text(row[0]) for row in event_rows)
    upsert_search_document(
        connection,
        message_id=message_id,
        chat_id=message_row[0],
        role="assistant",
        content=content,
        created_at=message_row[1],
    )


def sync_missing_search_documents(connection: sqlite3.Connection) -> None:
    """Backfill existing transcripts when the search feature is first installed."""
    connection.execute(
        """
        DELETE FROM chat_search_documents
        WHERE message_id NOT IN (SELECT id FROM chat_messages)
        """
    )

    user_rows = connection.execute(
        """
        SELECT m.id, m.chat_id, m.content, m.created_at
        FROM chat_messages AS m
        LEFT JOIN chat_search_documents AS d ON d.message_id = m.id
        WHERE m.role = 'user' AND d.message_id IS NULL
        """
    ).fetchall()
    for message_id, chat_id, content, created_at in user_rows:
        upsert_search_document(
            connection,
            message_id=message_id,
            chat_id=chat_id,
            role="user",
            content=content or "",
            created_at=created_at,
        )

    assistant_rows = connection.execute(
        """
        SELECT m.id
        FROM chat_messages AS m
        LEFT JOIN chat_search_documents AS d ON d.message_id = m.id
        WHERE m.role = 'assistant' AND d.message_id IS NULL
        """
    ).fetchall()
    for (message_id,) in assistant_rows:
        refresh_assistant_search_document(connection, message_id)


def fts_table_available(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'chat_search_fts'
        """
    ).fetchone()
    return row is not None


def search_rows_to_dicts(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [
        {
            "message_id": row[0],
            "chat_id": row[1],
            "chat_title": row[2],
            "role": row[3],
            "content": row[4],
            "created_at": row[5],
            "snippet": row[6],
        }
        for row in rows
    ]
