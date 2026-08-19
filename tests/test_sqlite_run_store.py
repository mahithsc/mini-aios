from __future__ import annotations

from pathlib import Path

from aios_core.db import get_db_connection, initialize_app_db
from aios_core.execution.service import build_run_event
from aios_core.execution.store import FileRunStore, SQLiteRunStore
from server.types.run import RunCreateRequest


def _create_chat(db_path: str, chat_id: str = "chat-1") -> None:
    initialize_app_db(db_path)
    with get_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO chats (id, title, status, created_at, updated_at)
            VALUES (?, NULL, 'streaming', 1, 1)
            """,
            (chat_id,),
        )


def test_chat_run_and_canonical_turn_commit_together(tmp_path: Path) -> None:
    db_path = str(tmp_path / "aios.db")
    _create_chat(db_path)
    store = SQLiteRunStore(db_path)

    run = store.create_run(
        RunCreateRequest(
            kind="chat",
            chatId="chat-1",
            sourceId="message-1",
            turnId="turn-1",
        )
    )

    with get_db_connection(db_path) as conn:
        migration = conn.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = 7"
        ).fetchone()
        stored_run = conn.execute(
            "SELECT status, chat_id, source_id, turn_id FROM runs WHERE id = ?",
            (run.id,),
        ).fetchone()
        stored_turn = conn.execute(
            """
            SELECT status, chat_id, user_message_id, run_id
            FROM conversation_turns WHERE turn_id = 'turn-1'
            """
        ).fetchone()

    assert tuple(migration) == ("durable_runs", "runs-v1")
    assert tuple(stored_run) == ("queued", "chat-1", "message-1", "turn-1")
    assert tuple(stored_turn) == ("queued", "chat-1", "message-1", run.id)


def test_run_events_and_snapshot_survive_store_recreation(tmp_path: Path) -> None:
    db_path = str(tmp_path / "aios.db")
    _create_chat(db_path)
    store = SQLiteRunStore(db_path)
    run = store.create_run(
        RunCreateRequest(
            kind="chat",
            chatId="chat-1",
            sourceId="message-1",
            turnId="turn-1",
        )
    )

    started, _ = store.record_event(
        run.id,
        build_run_event(
            run_id=run.id,
            event_type="started",
            chat_id="chat-1",
        ),
        status="running",
        active_step="model",
    )
    token, snapshot = store.record_event(
        run.id,
        build_run_event(
            run_id=run.id,
            event_type="token",
            chat_id="chat-1",
            data={"value": "hello"},
        ),
        status="running",
        preview="hello",
    )

    reopened = SQLiteRunStore(db_path)

    assert started.sequence == 1
    assert token.sequence == 2
    assert snapshot.lastSequence == 2
    assert reopened.get_snapshot(run.id) == snapshot
    assert [event.event.type for event in reopened.list_events_after(run.id, 0)] == [
        "started",
        "token",
    ]
    assert [event.event.type for event in reopened.list_events_after(run.id, 1)] == [
        "token"
    ]


def test_legacy_file_import_is_idempotent_and_never_overwrites_sql(
    tmp_path: Path,
) -> None:
    db_path = str(tmp_path / "aios.db")
    _create_chat(db_path)
    legacy = FileRunStore(
        metadata_dir=tmp_path / "metadata",
        snapshots_dir=tmp_path / "snapshots",
        events_dir=tmp_path / "events",
    )
    legacy_run = legacy.create_run(
        RunCreateRequest(kind="chat", chatId="chat-1", turnId="message-1")
    )
    legacy_event = legacy.append_event(
        legacy_run.id,
        build_run_event(
            run_id=legacy_run.id,
            event_type="started",
            chat_id="chat-1",
        ),
    )
    legacy.save_snapshot(
        legacy_run.id,
        status="running",
        last_sequence=legacy_event.sequence,
    )

    store = SQLiteRunStore(db_path)
    assert store.import_file_store(legacy) == 1
    store.save_snapshot(
        legacy_run.id,
        status="completed",
        last_sequence=legacy_event.sequence,
    )

    assert store.import_file_store(legacy) == 0
    assert store.get_snapshot(legacy_run.id).status == "completed"
