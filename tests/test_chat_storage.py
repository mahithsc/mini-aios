from __future__ import annotations

import json

import pytest

from aios_core import sessions
from aios_core.db import get_db_connection, initialize_app_db
from server.types.chat import AssistantMessage, UserMessage


@pytest.fixture
def isolated_chat_storage(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "data"
    session_dir = workspace_dir / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / "session_manifest.json").write_text("[]", encoding="utf-8")
    db_path = workspace_dir / "aios.db"

    monkeypatch.setattr(sessions, "DB_PATH", str(db_path))
    monkeypatch.setattr(sessions, "SESSION_DIR", session_dir)
    monkeypatch.setattr(
        sessions,
        "SESSION_MANIFEST_PATH",
        session_dir / "session_manifest.json",
    )
    monkeypatch.setattr(
        sessions,
        "_LEGACY_DEV_SESSION_DIR",
        tmp_path / "missing-legacy-session",
    )
    monkeypatch.setattr(
        sessions,
        "_LEGACY_ROOT_SESSION_DIR",
        tmp_path / "missing-root-session",
    )
    monkeypatch.setattr(
        sessions,
        "_LEGACY_SQLITE_CHAT_DB",
        tmp_path / "missing-state" / "aios.db",
    )
    monkeypatch.delenv("AIOS_STATE_DIR", raising=False)
    monkeypatch.delenv("AIOS_HOME", raising=False)
    monkeypatch.setattr(sessions, "get_data_dir", lambda: workspace_dir)
    sessions._CHAT_STORAGE_READY.clear()

    yield workspace_dir, db_path

    sessions._CHAT_STORAGE_READY.clear()


def test_chat_storage_nests_scratch_and_uploads_under_the_session(
    isolated_chat_storage,
):
    data_dir, _ = isolated_chat_storage

    sessions.create_chat("chat/unsafe")
    chat_segment = sessions.get_chat_session_relative_dir("chat/unsafe").parts[1]

    assert sessions.get_chat_session_relative_dir("chat/unsafe").as_posix() == (
        f"sessions/{chat_segment}"
    )
    assert sessions.get_chat_scratch_relative_dir("chat/unsafe").as_posix() == (
        f"sessions/{chat_segment}/scratch"
    )
    assert sessions.get_chat_uploads_relative_dir("chat/unsafe").as_posix() == (
        f"sessions/{chat_segment}/uploads"
    )
    assert chat_segment.startswith("chat-unsafe-")
    assert chat_segment != sessions.get_chat_session_relative_dir("chat-unsafe").parts[1]
    assert (data_dir / "sessions" / chat_segment / "scratch").is_dir()
    assert (data_dir / "sessions" / chat_segment / "uploads").is_dir()


def test_unsafe_chat_ids_have_collision_resistant_storage_paths() -> None:
    assert sessions.get_chat_scratch_relative_dir(
        "chat/a"
    ) != sessions.get_chat_scratch_relative_dir("chat-a")


def test_loading_db_only_chat_creates_its_filesystem_roots(
    isolated_chat_storage,
):
    data_dir, db_path = isolated_chat_storage
    initialize_app_db(str(db_path))
    with get_db_connection(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO chats (id, title, status, created_at, updated_at)
            VALUES ('db-only', NULL, 'idle', 1, 1)
            """
        )

    assert not (data_dir / "sessions/db-only").exists()
    assert sessions.load_chat_session("db-only") == []
    assert (data_dir / "sessions/db-only/scratch").is_dir()
    assert (data_dir / "sessions/db-only/uploads").is_dir()


@pytest.mark.parametrize(
    "file_path",
    [
        "../outside.txt",
        "workspace/../../outside.txt",
        "data:/../outside.txt",
        "data:../outside.txt",
        "data://outside.txt",
        "scratch:/../outside.txt",
        "scratch://outside.txt",
        "scratch:outside.txt",
    ],
)
def test_unsafe_attachment_paths_are_quarantined_to_chat_storage(file_path):
    chat_segment = sessions.get_chat_uploads_relative_dir("chat/unsafe").parts[1]
    assert sessions._canonical_attachment_path("chat/unsafe", file_path) == (
        f"sessions/{chat_segment}/uploads/.invalid-path"
    )


def test_import_does_not_read_unsafe_relative_attachment_path(tmp_path):
    source_workspace = tmp_path / "source"
    target_workspace = tmp_path / "target"
    source_workspace.mkdir()
    target_workspace.mkdir()
    (tmp_path / "outside.txt").write_text("private", encoding="utf-8")
    message = UserMessage(
        id="unsafe-message",
        content="See file",
        status="complete",
        createdAt=1000,
        updatedAt=1000,
        attachments=[
            {
                "id": "unsafe-attachment",
                "kind": "file",
                "name": "outside.txt",
                "filePath": "../outside.txt",
                "mimeType": "text/plain",
                "sizeBytes": 7,
                "uploadedAt": 900,
            }
        ],
    )

    remapped = sessions._remap_imported_attachment_paths(
        "chat-1",
        [message],
        source_workspace=source_workspace,
        target_workspace=target_workspace,
    )

    assert remapped[0].attachments[0].filePath == (
        "sessions/chat-1/uploads/.invalid-path"
    )
    assert not (target_workspace / "sessions/chat-1/uploads/.invalid-path").exists()


def test_new_and_existing_attachment_rows_are_canonicalized(
    isolated_chat_storage,
):
    _, db_path = isolated_chat_storage
    assert sessions._canonical_attachment_path(
        "chat-1",
        "workspace/session/chat-1/files/derived.txt",
    ) == "sessions/chat-1/scratch/derived.txt"
    sessions.create_chat("chat-1")
    sessions.append_user_message(
        "chat-1",
        UserMessage(
            id="user-legacy-path",
            content="See file",
            status="complete",
            createdAt=1000,
            updatedAt=1000,
            attachments=[
                {
                    "id": "attachment-legacy-path",
                    "kind": "file",
                    "name": "one.txt",
                    "filePath": "data:/uploads/chat-1/one.txt",
                    "mimeType": "text/plain",
                    "sizeBytes": 3,
                    "uploadedAt": 900,
                }
            ],
        ),
    )

    with get_db_connection(str(db_path)) as conn:
        stored = conn.execute(
            "SELECT file_path FROM message_attachments WHERE id = ?",
            ("attachment-legacy-path",),
        ).fetchone()[0]
        assert stored == "sessions/chat-1/uploads/one.txt"
        conn.execute(
            "UPDATE message_attachments SET file_path = ? WHERE id = ?",
            (
                "workspace/session/chat-1/uploads/two.txt",
                "attachment-legacy-path",
            ),
        )
        conn.execute(
            """
            INSERT INTO attachment_representations
                (id, attachment_id, position, kind, status, file_path,
                 metadata_json, created_at, updated_at)
            VALUES (?, ?, 0, 'preview', 'ready', ?, '{}', 1000, 1000)
            """,
            (
                "representation-legacy-path",
                "attachment-legacy-path",
                "session/chat-1/artifacts/preview/index.html",
            ),
        )

    assert sessions._canonicalize_stored_attachment_paths(str(db_path)) == 2
    assert sessions._canonicalize_stored_attachment_paths(str(db_path)) == 0
    assert sessions.load_chat_session("chat-1")[0].attachments[0].filePath == (
        "sessions/chat-1/uploads/two.txt"
    )
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute(
            "SELECT file_path FROM attachment_representations WHERE id = ?",
            ("representation-legacy-path",),
        ).fetchone()[0] == "sessions/chat-1/uploads/.invalid-path"


def test_legacy_database_discovery_includes_core_migration_archive(
    isolated_chat_storage,
):
    data_dir, _ = isolated_chat_storage
    archive_dir = data_dir / "legacy" / "storage-layout-v1" / "state"
    archive_dir.mkdir(parents=True)
    archived_db = archive_dir / "aios.db"
    conflict_db = archive_dir / "aios.db.conflict-1"
    archived_db.touch()
    conflict_db.touch()
    (archive_dir / "aios.db-wal").touch()

    candidates = sessions._legacy_sqlite_chat_db_candidates()

    assert archived_db.resolve() in candidates
    assert conflict_db.resolve() in candidates
    assert (archive_dir / "aios.db-wal").resolve() not in candidates


def test_explicit_data_dir_isolates_legacy_chat_discovery(
    isolated_chat_storage,
    tmp_path,
    monkeypatch,
):
    data_dir, _ = isolated_chat_storage
    archived_state_dir = data_dir / "legacy" / "storage-layout-v1" / "state"
    archived_state_dir.mkdir(parents=True)
    archived_db = archived_state_dir / "aios.db"
    archived_db.touch()

    checkout_db = tmp_path / "checkout" / "state" / "aios.db"
    checkout_db.parent.mkdir(parents=True)
    checkout_db.touch()
    checkout_session = tmp_path / "checkout" / "session"
    checkout_workspace_session = tmp_path / "checkout" / "workspace" / "session"
    checkout_session.mkdir(parents=True)
    checkout_workspace_session.mkdir(parents=True)

    configured_state = tmp_path / "configured-state"
    configured_home = tmp_path / "configured-home"
    monkeypatch.setenv("AIOS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AIOS_STATE_DIR", str(configured_state))
    monkeypatch.setenv("AIOS_HOME", str(configured_home))
    monkeypatch.setattr(sessions, "_LEGACY_SQLITE_CHAT_DB", checkout_db)
    monkeypatch.setattr(sessions, "_LEGACY_ROOT_SESSION_DIR", checkout_session)
    monkeypatch.setattr(
        sessions,
        "_LEGACY_DEV_SESSION_DIR",
        checkout_workspace_session,
    )

    assert sessions._legacy_sqlite_chat_db_candidates() == [archived_db.resolve()]
    assert sessions._legacy_chat_session_candidates() == [
        sessions.SESSION_DIR.resolve()
    ]


def test_json_import_is_idempotent_and_preserves_structured_events(
    isolated_chat_storage,
):
    workspace_dir, db_path = isolated_chat_storage
    legacy_workspace = workspace_dir / "workspace"
    legacy_session = legacy_workspace / "session"
    chat_dir = legacy_session / "chat-1"
    chat_dir.mkdir(parents=True)
    (legacy_session / "session_manifest.json").write_text(
        json.dumps(
            [
                {
                    "id": "chat-1",
                    "file": "chat-1/chat.json",
                    "status": "idle",
                    "addedAt": "2026-01-01T12:00:00",
                    "title": "Imported chat",
                }
            ]
        ),
        encoding="utf-8",
    )
    (chat_dir / "chat.json").write_text(
        json.dumps(
            [
                {
                    "id": "user-1",
                    "role": "user",
                    "content": "Read the report",
                    "status": "complete",
                    "createdAt": 1000,
                    "updatedAt": 1000,
                    "attachments": [
                        {
                            "id": "attachment-1",
                            "kind": "file",
                            "name": "report.pdf",
                            "filePath": "session/chat-1/uploads/report.pdf",
                            "mimeType": "application/pdf",
                            "sizeBytes": 123,
                            "uploadedAt": 900,
                        }
                    ],
                },
                {
                    "id": "assistant-1",
                    "role": "assistant",
                    "runId": "run-1",
                    "status": "complete",
                    "createdAt": 1100,
                    "updatedAt": 1400,
                    "events": [
                        {
                            "id": "event-1",
                            "type": "stream_start",
                            "createdAt": 1100,
                        },
                        {
                            "id": "event-2",
                            "type": "tool_call_start",
                            "toolCallId": "tool-1",
                            "toolName": "read",
                            "input": {"path": "report.pdf"},
                            "createdAt": 1200,
                        },
                        {
                            "id": "event-3",
                            "type": "tool_call_end",
                            "toolCallId": "tool-1",
                            "toolName": "read",
                            "output": {"pages": 2},
                            "createdAt": 1300,
                        },
                        {
                            "id": "event-4",
                            "type": "stream_end",
                            "createdAt": 1400,
                        },
                    ],
                },
            ]
        ),
        encoding="utf-8",
    )

    first = sessions.migrate_legacy_chat_sessions(
        legacy_session,
        db_path=str(db_path),
        target_workspace=workspace_dir,
    )
    second = sessions.migrate_legacy_chat_sessions(
        legacy_session,
        db_path=str(db_path),
        target_workspace=workspace_dir,
    )

    assert first.chat_count == 1
    assert first.message_count == 2
    assert first.event_count == 4
    assert first.attachment_count == 1
    assert second.already_imported is True

    messages = sessions.load_chat_session("chat-1")
    assert len(messages) == 2
    assert isinstance(messages[0], UserMessage)
    assert messages[0].attachments[0].filePath == (
        "sessions/chat-1/uploads/report.pdf"
    )
    assert isinstance(messages[1], AssistantMessage)
    assert [event.type for event in messages[1].events] == [
        "stream_start",
        "tool_call_start",
        "tool_call_end",
        "stream_end",
    ]

    # A source path is a retry checkpoint, not a permanent one-shot marker.
    # If an older branch adds a later turn, a subsequent scan imports it.
    transcript_path = chat_dir / "chat.json"
    updated_transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    updated_transcript.append(
        {
            "id": "user-2",
            "role": "user",
            "content": "One more question",
            "status": "complete",
            "createdAt": 1500,
            "updatedAt": 1500,
            "attachments": [],
        }
    )
    transcript_path.write_text(json.dumps(updated_transcript), encoding="utf-8")
    third = sessions.migrate_legacy_chat_sessions(
        legacy_session,
        db_path=str(db_path),
        target_workspace=workspace_dir,
    )
    assert third.already_imported is True
    assert third.chat_count == 1
    assert [message.id for message in sessions.load_chat_session("chat-1")] == [
        "user-1",
        "assistant-1",
        "user-2",
    ]


def test_incremental_events_do_not_replace_the_transcript(isolated_chat_storage):
    _, db_path = isolated_chat_storage
    sessions.create_chat("chat-1")
    appended = sessions.append_user_message(
        "chat-1",
        UserMessage(
            id="user-1",
            content="Hello",
            status="complete",
            createdAt=1000,
            updatedAt=1000,
        ),
        chat_status="streaming",
    )
    assert appended is True

    events = [
        {"id": "run-1:1", "type": "stream_start", "createdAt": 1100},
        {"id": "run-1:2", "type": "token", "value": "Hi", "createdAt": 1200},
        {"id": "run-1:3", "type": "token", "value": " there", "createdAt": 1300},
        {"id": "run-1:4", "type": "stream_end", "createdAt": 1400},
    ]
    for event in events:
        assert sessions.append_assistant_event("chat-1", "run-1", event) is True

    assert sessions.append_assistant_event("chat-1", "run-1", events[-1]) is False

    messages = sessions.load_chat_session("chat-1")
    assert len(messages) == 2
    assert isinstance(messages[1], AssistantMessage)
    assert messages[1].status == "complete"
    assert [event.type for event in messages[1].events] == [
        "stream_start",
        "token",
        "token",
        "stream_end",
    ]
    assert "".join(
        event.value for event in messages[1].events if event.type == "token"
    ) == "Hi there"
    assert sessions.get_chat_metadata("chat-1").status == "idle"

    with get_db_connection(str(db_path)) as conn:
        assert conn.execute(
            "SELECT count(*) FROM assistant_events WHERE message_id = ?",
            (messages[1].id,),
        ).fetchone()[0] == 4


def test_sqlite_import_is_idempotent_and_keeps_destination_authoritative(
    isolated_chat_storage,
):
    workspace_dir, db_path = isolated_chat_storage
    source_db = workspace_dir.parent / "legacy-state.db"
    initialize_app_db(str(source_db))
    with get_db_connection(str(source_db)) as conn:
        # Older versions predate the optional attachment hash column.
        conn.execute("ALTER TABLE message_attachments DROP COLUMN content_hash")

    sessions.create_chat("existing-chat", "Destination title")
    sessions.append_user_message(
        "existing-chat",
        UserMessage(
            id="destination-user",
            content="Keep canonical history",
            status="complete",
            createdAt=100,
            updatedAt=100,
        ),
    )
    with get_db_connection(str(source_db)) as conn:
        conn.executemany(
            """
            INSERT INTO chats (id, title, status, created_at, updated_at)
            VALUES (?, ?, 'idle', ?, ?)
            """,
            [
                ("existing-chat", "Source title", 100, 100),
                ("imported-chat", "Imported title", 200, 500),
            ],
        )
        conn.execute(
            """
            INSERT INTO chat_messages
                (id, chat_id, position, role, content, run_id, status,
                 created_at, updated_at)
            VALUES (
                'source-existing-user', 'existing-chat', 0, 'user',
                'Do not replace canonical history', NULL, 'complete', 100, 100
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO chat_messages
                (id, chat_id, position, role, content, run_id, status,
                 created_at, updated_at)
            VALUES (?, 'imported-chat', ?, ?, ?, ?, 'complete', ?, ?)
            """,
            [
                ("imported-user", 0, "user", "Hello from state", None, 200, 200),
                ("imported-assistant", 1, "assistant", None, "imported-run", 300, 500),
            ],
        )
        conn.execute(
            """
            INSERT INTO message_attachments
                (id, message_id, position, kind, name, file_path, mime_type,
                 size_bytes, uploaded_at)
            VALUES (
                'imported-file', 'imported-user', 0, 'file', 'report.pdf',
                'workspace/session/imported-chat/uploads/report.pdf',
                'application/pdf', 123, 190
            )
            """
        )
        conn.execute(
            """
            INSERT INTO attachment_representations
                (id, attachment_id, position, kind, status, text_content,
                 file_path, mime_type, metadata_json, created_at, updated_at)
            VALUES (
                'imported-preview', 'imported-file', 0, 'preview', 'ready', NULL,
                'workspace/session/imported-chat/artifacts/preview.txt',
                'text/plain', '{}', 200, 200
            )
            """
        )
        conn.execute(
            """
            INSERT INTO assistant_events
                (id, message_id, sequence, type, payload_json, created_at)
            VALUES (
                'imported-event', 'imported-assistant', 0, 'token',
                '{"value":"Hello back"}', 500
            )
            """
        )

    first = sessions.migrate_legacy_chat_database(
        source_db,
        db_path=str(db_path),
    )
    second = sessions.migrate_legacy_chat_database(
        source_db,
        db_path=str(db_path),
    )

    assert first.chat_count == 1
    assert first.message_count == 2
    assert first.event_count == 1
    assert first.attachment_count == 1
    assert first.skipped_count == 1
    assert second.already_imported is True
    assert sessions.get_chat_metadata("existing-chat").title == "Destination title"

    imported = sessions.load_chat_session("imported-chat")
    assert len(imported) == 2
    assert isinstance(imported[0], UserMessage)
    assert imported[0].attachments[0].filePath == (
        "sessions/imported-chat/uploads/report.pdf"
    )
    assert isinstance(imported[1], AssistantMessage)
    assert imported[1].events[0].value == "Hello back"
    with get_db_connection(str(db_path)) as conn:
        representation_path = conn.execute(
            """
            SELECT file_path
            FROM attachment_representations
            WHERE id = 'imported-preview'
            """
        ).fetchone()[0]
        assert representation_path == "sessions/imported-chat/uploads/.invalid-path"

    # The legacy state database is a read-only source from the migration's
    # perspective; its original path layout remains unchanged.
    with get_db_connection(str(source_db)) as conn:
        assert conn.execute(
            "SELECT file_path FROM message_attachments WHERE id = 'imported-file'"
        ).fetchone()[0] == "workspace/session/imported-chat/uploads/report.pdf"
        source_representation_path = conn.execute(
            """
            SELECT file_path
            FROM attachment_representations
            WHERE id = 'imported-preview'
            """
        ).fetchone()[0]
        assert source_representation_path == (
            "workspace/session/imported-chat/artifacts/preview.txt"
        )

        conn.execute(
            """
            INSERT INTO chats (id, title, status, created_at, updated_at)
            VALUES ('later-chat', 'Created later', 'idle', 700, 700)
            """
        )

    third = sessions.migrate_legacy_chat_database(source_db, db_path=str(db_path))
    assert third.already_imported is True
    assert third.chat_count == 1
    assert sessions.get_chat_metadata("later-chat").title == "Created later"

    destination_updated_at = sessions.get_chat_metadata("existing-chat").updatedAt
    with get_db_connection(str(source_db)) as conn:
        conn.execute(
            "UPDATE chats SET updated_at = ? WHERE id = 'existing-chat'",
            (destination_updated_at + 1,),
        )
    sessions.migrate_legacy_chat_database(source_db, db_path=str(db_path))
    assert sessions.get_chat_metadata("existing-chat").title == "Destination title"
    assert [message.id for message in sessions.load_chat_session("existing-chat")] == [
        "destination-user"
    ]


def test_corrupt_json_chat_is_retried_after_repair(isolated_chat_storage):
    workspace_dir, db_path = isolated_chat_storage
    legacy_session = workspace_dir / "legacy-session"
    chat_dir = legacy_session / "repairable-chat"
    chat_dir.mkdir(parents=True)
    transcript_path = chat_dir / "chat.json"
    transcript_path.write_text("{not valid json", encoding="utf-8")

    failed = sessions.migrate_legacy_chat_sessions(
        legacy_session,
        db_path=str(db_path),
        target_workspace=workspace_dir,
    )
    assert failed.skipped_count == 1
    assert sessions.get_chat_metadata("repairable-chat") is None
    with get_db_connection(str(db_path)) as conn:
        assert conn.execute(
            "SELECT 1 FROM chat_imports WHERE source_path = ?",
            (str(legacy_session.resolve()),),
        ).fetchone() is None

    transcript_path.write_text(
        json.dumps(
            [
                {
                    "id": "repaired-user",
                    "role": "user",
                    "content": "Recovered",
                    "status": "complete",
                    "createdAt": 1000,
                    "updatedAt": 1000,
                    "attachments": [],
                }
            ]
        ),
        encoding="utf-8",
    )
    repaired = sessions.migrate_legacy_chat_sessions(
        legacy_session,
        db_path=str(db_path),
        target_workspace=workspace_dir,
    )
    assert repaired.chat_count == 1
    assert sessions.load_chat_session("repairable-chat")[0].content == "Recovered"


def test_legacy_upload_is_copied_into_chat_sandbox(isolated_chat_storage):
    workspace_dir, db_path = isolated_chat_storage
    legacy_session = workspace_dir / "old-session"
    chat_dir = legacy_session / "upload-chat"
    chat_dir.mkdir(parents=True)
    source_file = workspace_dir / "uploads" / "upload-chat" / "notes.txt"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("preserve me", encoding="utf-8")
    (chat_dir / "chat.json").write_text(
        json.dumps(
            [
                {
                    "id": "upload-user",
                    "role": "user",
                    "content": "See attachment",
                    "status": "complete",
                    "createdAt": 1000,
                    "updatedAt": 1000,
                    "attachments": [
                        {
                            "id": "legacy-upload",
                            "kind": "file",
                            "name": "notes.txt",
                            "filePath": "uploads/upload-chat/notes.txt",
                            "mimeType": "text/plain",
                            "sizeBytes": 11,
                            "uploadedAt": 900,
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    report = sessions.migrate_legacy_chat_sessions(
        legacy_session,
        db_path=str(db_path),
        target_workspace=workspace_dir,
    )
    attachment = sessions.load_chat_session("upload-chat")[0].attachments[0]
    copied_file = workspace_dir / attachment.filePath

    assert report.attachment_count == 1
    assert attachment.filePath == "sessions/upload-chat/uploads/notes.txt"
    assert copied_file.read_text(encoding="utf-8") == "preserve me"
    assert source_file.read_text(encoding="utf-8") == "preserve me"
