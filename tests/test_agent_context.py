from __future__ import annotations

from pathlib import Path

import pytest

from aios_core.agent import context


def test_agent_paths_default_to_scratch_and_support_explicit_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / ".mini-aios"
    scratch_dir = data_dir / "sessions" / "chat-1" / "scratch"
    monkeypatch.setattr(context, "get_current_chat_scratch_dir", lambda: scratch_dir)
    monkeypatch.setattr(
        context,
        "resolve_workspace_path",
        lambda path: data_dir / Path(path),
    )

    assert context.resolve_agent_path("notes.txt") == scratch_dir / "notes.txt"
    assert context.resolve_agent_path("scratch:/notes.txt") == scratch_dir / "notes.txt"
    assert context.resolve_agent_path("data:/projects/app_1") == (
        data_dir / "projects" / "app_1"
    )
    assert context.resolve_agent_path("projects/app_1") == (
        data_dir / "projects" / "app_1"
    )
    assert context.resolve_agent_path("session/chat-1/files/old.txt") == (
        data_dir / "sessions" / "chat-1" / "scratch" / "old.txt"
    )
    assert context.resolve_agent_path(
        "data:/workspace/session/chat-1/uploads/old.txt"
    ) == (data_dir / "uploads" / "chat-1" / "old.txt")
    assert context.resolve_agent_path("sessions/chat-1/artifacts/index.html") == (
        data_dir / "artifacts" / "chat-1" / "index.html"
    )


def test_explicit_scratch_scope_requires_an_active_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context, "get_current_chat_scratch_dir", lambda: None)

    with pytest.raises(ValueError, match="active chat"):
        context.resolve_agent_path("scratch:/notes.txt")


@pytest.mark.parametrize("path", ["data:/../outside", "scratch://outside"])
def test_explicit_scopes_reject_escape_paths(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context, "get_current_chat_scratch_dir", lambda: Path("/scratch"))

    with pytest.raises(ValueError, match="cannot escape"):
        context.resolve_agent_path(path)
