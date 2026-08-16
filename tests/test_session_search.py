from __future__ import annotations

import pytest

from aios_core import sessions
from aios_core.tools.session_search import session_search
from server.types.chat import AssistantMessage, UserMessage


@pytest.fixture
def isolated_search_storage(tmp_path, monkeypatch):
    """Point the SQLite chat store and its legacy importer at a temp workspace."""
    workspace_dir = tmp_path / "workspace"
    session_dir = workspace_dir / "session"
    session_dir.mkdir(parents=True)
    (session_dir / "session_manifest.json").write_text("[]", encoding="utf-8")

    monkeypatch.setattr(sessions, "DB_PATH", str(workspace_dir / "aios.db"))
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
    monkeypatch.setattr(sessions, "get_workspace_dir", lambda: workspace_dir)
    sessions._CHAT_STORAGE_READY.clear()

    yield session_dir

    sessions._CHAT_STORAGE_READY.clear()


def _user(message_id: str, text: str, timestamp: int) -> UserMessage:
    return UserMessage(
        id=message_id,
        content=text,
        status="complete",
        createdAt=timestamp,
        updatedAt=timestamp,
    )


def _assistant(message_id: str, text: str, timestamp: int) -> AssistantMessage:
    half = len(text) // 2
    return AssistantMessage(
        id=message_id,
        runId=f"run-{message_id}",
        status="complete",
        createdAt=timestamp,
        updatedAt=timestamp + 3,
        events=[
            {"id": f"{message_id}:start", "type": "stream_start", "createdAt": timestamp},
            {"id": f"{message_id}:t1", "type": "token", "value": text[:half], "createdAt": timestamp + 1},
            {"id": f"{message_id}:t2", "type": "token", "value": text[half:], "createdAt": timestamp + 2},
            {"id": f"{message_id}:end", "type": "stream_end", "createdAt": timestamp + 3},
        ],
    )


def _save_turn(
    chat_id: str,
    *,
    user_id: str,
    user_text: str,
    assistant_id: str,
    assistant_text: str,
    timestamp: int,
) -> None:
    sessions.save_chat_session(
        chat_id,
        [
            _user(user_id, user_text, timestamp),
            _assistant(assistant_id, assistant_text, timestamp + 10),
        ],
    )


def test_session_search_indexes_user_and_streamed_assistant_text(
    isolated_search_storage,
) -> None:
    _save_turn(
        "chat-1",
        user_id="user-1",
        user_text="Use quasar for the production deployment.",
        assistant_id="assistant-1",
        assistant_text="The quasar deployment completed successfully.",
        timestamp=1_000,
    )
    _save_turn(
        "chat-2",
        user_id="user-2",
        user_text="Discuss the billing dashboard.",
        assistant_id="assistant-2",
        assistant_text="The dashboard work is scheduled for next week.",
        timestamp=2_000,
    )

    result = session_search(query="quasar deployment", limit=5)
    filtered = session_search(query="quasar deployment", chat_id="chat-2", limit=5)

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
    _save_turn(
        "chat-1",
        user_id="user-1",
        user_text="First message",
        assistant_id="assistant-1",
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


def test_whole_transcript_replacement_removes_stale_search_text(
    isolated_search_storage,
) -> None:
    _save_turn(
        "chat-1",
        user_id="user-old",
        user_text="The obsolete codename is marigold.",
        assistant_id="assistant-old",
        assistant_text="Marigold was recorded.",
        timestamp=1_000,
    )

    sessions.save_chat_session(
        "chat-1",
        [_user("user-new", "The current codename is juniper.", 2_000)],
    )

    old_result = session_search(query="marigold")
    new_result = session_search(query="juniper")

    assert old_result["results"] == []
    assert [item["message_id"] for item in new_result["results"]] == ["user-new"]
