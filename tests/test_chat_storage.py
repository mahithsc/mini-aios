from __future__ import annotations

import json

import pytest

from aios_core import sessions
from aios_core.db import get_db_connection
from server.types.chat import AssistantMessage, UserMessage


@pytest.fixture
def isolated_chat_storage(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "active-workspace"
    session_dir = workspace_dir / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "session_manifest.json").write_text("[]", encoding="utf-8")
    db_path = workspace_dir / "aios.db"

    monkeypatch.setattr(sessions, "DB_PATH", str(db_path))
    monkeypatch.setattr(sessions, "SESSION_DIR", session_dir)
    monkeypatch.setattr(
        sessions,
        "_LEGACY_DEV_SESSION_DIR",
        tmp_path / "missing-legacy-session",
    )
    monkeypatch.setattr(sessions, "get_workspace_dir", lambda: workspace_dir)
    sessions._CHAT_STORAGE_READY.clear()

    yield workspace_dir, db_path

    sessions._CHAT_STORAGE_READY.clear()


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
        "workspace/session/chat-1/uploads/report.pdf"
    )
    assert isinstance(messages[1], AssistantMessage)
    assert [event.type for event in messages[1].events] == [
        "stream_start",
        "tool_call_start",
        "tool_call_end",
        "stream_end",
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
