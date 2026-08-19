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
    ) == (data_dir / "sessions" / "chat-1" / "uploads" / "old.txt")
    assert context.resolve_agent_path("data:/uploads/chat-1/old.txt") == (
        data_dir / "sessions" / "chat-1" / "uploads" / "old.txt"
    )
    assert context.resolve_agent_path("data:/artifacts/chat-1/report.html") == (
        data_dir / "sessions" / "chat-1" / "artifacts" / "report.html"
    )
    assert context.resolve_agent_path(
        "session/chat-1/artifacts/report.html"
    ) == (data_dir / "sessions" / "chat-1" / "artifacts" / "report.html")


def test_explicit_scratch_scope_requires_an_active_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context, "get_current_chat_scratch_dir", lambda: None)

    with pytest.raises(ValueError, match="active chat"):
        context.resolve_agent_path("scratch:/notes.txt")


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "nested/../../outside",
        "data:/../outside",
        "data:../outside",
        "scratch://outside",
        "scratch:outside",
    ],
)
def test_explicit_scopes_reject_escape_paths(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        context, "get_current_chat_scratch_dir", lambda: Path("/scratch")
    )

    with pytest.raises(ValueError, match="cannot escape"):
        context.resolve_agent_path(path)


def test_absolute_agent_paths_remain_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        context,
        "get_current_chat_scratch_dir",
        lambda: tmp_path / "scratch",
    )

    assert context.resolve_agent_path(tmp_path / "external.txt") == (
        tmp_path / "external.txt"
    )


def test_runtime_context_ensures_chat_storage_before_publishing_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scratch_dir = tmp_path / "sessions" / "chat-1" / "scratch"
    artifacts_dir = tmp_path / "sessions" / "chat-1" / "artifacts"
    calls: list[str] = []
    monkeypatch.setattr(
        context,
        "ensure_chat_storage_dirs",
        lambda chat_id: calls.append(chat_id),
    )
    monkeypatch.setattr(context, "get_chat_scratch_dir", lambda _chat_id: scratch_dir)
    monkeypatch.setattr(
        context,
        "get_chat_artifacts_dir",
        lambda _chat_id: artifacts_dir,
    )

    tokens = context.push_chat_runtime_context("chat-1")
    try:
        assert calls == ["chat-1"]
        assert context.get_current_chat_scratch_dir() == scratch_dir
        assert context.get_current_chat_artifacts_dir() == artifacts_dir
    finally:
        context.pop_chat_runtime_context(tokens)
