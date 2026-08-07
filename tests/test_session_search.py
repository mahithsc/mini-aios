from __future__ import annotations

import pytest

from aios_core import sessions
from aios_core.db import get_db_connection, initialize_app_db
from aios_core.tools.session_search import session_search
from server.types.chat import UserMessage


@pytest.fixture
def isolated_search_storage(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "workspace"
    session_dir = workspace_dir / "session"
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
    monkeypatch.setattr(sessions, "get_workspace_dir", lambda: workspace_dir)
    sessions._CHAT_STORAGE_READY.clear()

    yield db_path

    sessions._CHAT_STORAGE_READY.clear()


def _append_turn(
    chat_id: str,
    *,
    user_id: str,
    user_text: str,
    run_id: str,
    assistant_text: str,
    timestamp: int,
) -> None:
    sessions.create_chat(chat_id)
    sessions.append_user_message(
        chat_id,
        UserMessage(
            id=user_id,
            content=user_text,
            status="complete",
            createdAt=timestamp,
            updatedAt=timestamp,
        ),
    )
    events = [
        {
            "id": f"{run_id}:start",
            "type": "stream_start",
            "createdAt": timestamp + 1,
        },
        {
            "id": f"{run_id}:token-1",
            "type": "token",
            "value": assistant_text[: len(assistant_text) // 2],
            "createdAt": timestamp + 2,
        },
        {
            "id": f"{run_id}:token-2",
            "type": "token",
            "value": assistant_text[len(assistant_text) // 2 :],
            "createdAt": timestamp + 3,
        },
        {
            "id": f"{run_id}:end",
            "type": "stream_end",
            "createdAt": timestamp + 4,
        },
    ]
    for event in events:
        sessions.append_assistant_event(chat_id, run_id, event)


def test_session_search_indexes_user_and_streamed_assistant_text(
    isolated_search_storage,
) -> None:
    _append_turn(
        "chat-1",
        user_id="user-1",
        user_text="Use quasar for the production deployment.",
        run_id="run-1",
        assistant_text="The quasar deployment completed successfully.",
        timestamp=1_000,
    )
    _append_turn(
        "chat-2",
        user_id="user-2",
        user_text="Discuss the billing dashboard.",
        run_id="run-2",
        assistant_text="The dashboard work is scheduled for next week.",
        timestamp=2_000,
    )

    result = session_search(query="quasar deployment", limit=5)
    filtered = session_search(
        query="quasar deployment",
        chat_id="chat-2",
        limit=5,
    )

    assert result["mode"] == "search"
    assert result["results"]
    assert {item["chat_id"] for item in result["results"]} == {"chat-1"}
    assert {item["role"] for item in result["results"]} == {"user", "assistant"}
    assert any(
        "completed successfully" in item["content"] for item in result["results"]
    )
    assert filtered["results"] == []


def test_session_search_can_browse_and_list_recent_chats(
    isolated_search_storage,
) -> None:
    _append_turn(
        "chat-1",
        user_id="user-1",
        user_text="First message",
        run_id="run-1",
        assistant_text="First response",
        timestamp=1_000,
    )

    browse = session_search(chat_id="chat-1", limit=10)
    recent = session_search(limit=10)

    assert browse["mode"] == "browse_chat"
    assert [item["role"] for item in browse["results"]] == ["user", "assistant"]
    assert browse["results"][1]["content"] == "First response"
    assert recent["mode"] == "recent_chats"
    assert recent["results"][0]["chat_id"] == "chat-1"
    assert recent["results"][0]["message_count"] == 2


def test_database_initialization_backfills_existing_messages(
    isolated_search_storage,
) -> None:
    db_path = str(isolated_search_storage)
    initialize_app_db(db_path)
    with get_db_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO chats (id, title, status, created_at, updated_at)
            VALUES ('legacy-chat', 'Legacy', 'idle', 100, 100)
            """
        )
        connection.execute(
            """
            INSERT INTO chat_messages
                (id, chat_id, position, role, content, run_id, status,
                 created_at, updated_at)
            VALUES
                ('legacy-user', 'legacy-chat', 0, 'user',
                 'The cobalt migration happened in March.', NULL,
                 'complete', 100, 100)
            """
        )

    initialize_app_db(db_path)
    result = session_search(query="cobalt migration")

    assert len(result["results"]) == 1
    assert result["results"][0]["message_id"] == "legacy-user"


def test_whole_transcript_replacement_removes_stale_search_text(
    isolated_search_storage,
) -> None:
    _append_turn(
        "chat-1",
        user_id="user-old",
        user_text="The obsolete codename is marigold.",
        run_id="run-old",
        assistant_text="Marigold was recorded.",
        timestamp=1_000,
    )

    sessions.save_chat_session(
        "chat-1",
        [
            UserMessage(
                id="user-new",
                content="The current codename is juniper.",
                status="complete",
                createdAt=2_000,
                updatedAt=2_000,
            )
        ],
    )

    old_result = session_search(query="marigold")
    new_result = session_search(query="juniper")

    assert old_result["results"] == []
    assert [item["message_id"] for item in new_result["results"]] == ["user-new"]
