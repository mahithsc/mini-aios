from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from aios_core.agent.events import normalize_tool_output


@pytest.fixture
def artifact_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = importlib.import_module("aios_core.agent.tools.artifact_tool")
    data_root = tmp_path / ".mini-aios"
    artifacts_root = data_root / "sessions" / "chat-1" / "artifacts"

    monkeypatch.setattr(module, "get_current_chat_id", lambda: "chat-1")
    monkeypatch.setattr(
        module,
        "get_current_chat_artifacts_dir",
        lambda: artifacts_root,
    )
    monkeypatch.setattr(module, "get_data_dir", lambda: data_root)
    monkeypatch.setattr(
        module,
        "get_chat_artifacts_relative_dir",
        lambda _chat_id: Path("sessions/chat-1/artifacts"),
    )

    def ensure(_chat_id: str) -> Path:
        artifacts_root.mkdir(parents=True, exist_ok=True)
        return artifacts_root

    monkeypatch.setattr(module, "ensure_chat_artifacts_dir", ensure)
    monkeypatch.setattr(module, "DEFAULT_SERVER_BASE_URL", "http://localhost:8765")
    return module, artifacts_root


def test_artifact_creates_lazy_nested_directory_and_descriptor(artifact_tool) -> None:
    module, artifacts_root = artifact_tool
    assert not artifacts_root.exists()

    result = module.artifact("report/index.html", "<h1>Ready</h1>")

    assert result["ok"] is True
    assert (artifacts_root / "report/index.html").read_text(encoding="utf-8") == (
        "<h1>Ready</h1>"
    )
    assert result["artifact"] == {
        "version": 1,
        "chatId": "chat-1",
        "title": "index.html",
        "path": "report/index.html",
        "filePath": "sessions/chat-1/artifacts/report/index.html",
        "dataPath": "data:/sessions/chat-1/artifacts/report/index.html",
        "url": "http://localhost:8765/session-artifacts/chat-1/report/index.html",
        "mimeType": "text/html",
        "sizeBytes": 14,
    }


def test_artifact_atomically_overwrites_existing_file(artifact_tool) -> None:
    module, artifacts_root = artifact_tool

    assert module.artifact("notes.txt", "first")["ok"] is True
    assert module.artifact("notes.txt", "second")["ok"] is True

    assert (artifacts_root / "notes.txt").read_text(encoding="utf-8") == "second"


@pytest.mark.parametrize(
    "path",
    ["", ".", "../secret.txt", "nested/../../secret.txt", "/tmp/secret.txt", "C:\\secret.txt"],
)
def test_artifact_rejects_paths_outside_the_active_session(
    artifact_tool,
    path: str,
) -> None:
    module, artifacts_root = artifact_tool

    result = module.artifact(path, "secret")

    assert result["ok"] is False
    assert not artifacts_root.exists()


def test_artifact_rejects_symlink_escape(artifact_tool, tmp_path: Path) -> None:
    module, artifacts_root = artifact_tool
    artifacts_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (artifacts_root / "linked").symlink_to(outside, target_is_directory=True)

    result = module.artifact("linked/secret.txt", "secret")

    assert result == {
        "ok": False,
        "error": "path cannot escape the artifacts directory",
    }
    assert not (outside / "secret.txt").exists()


def test_artifact_requires_active_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("aios_core.agent.tools.artifact_tool")
    monkeypatch.setattr(module, "get_current_chat_id", lambda: None)
    monkeypatch.setattr(module, "get_current_chat_artifacts_dir", lambda: None)

    assert module.artifact("report.txt", "content") == {
        "ok": False,
        "error": "artifact requires an active chat",
    }


def test_artifact_tool_output_is_normalized_for_structured_events() -> None:
    output = {"ok": True, "type": "session_artifact", "artifact": {"path": "a.html"}}

    assert normalize_tool_output("artifact", json.dumps(output)) == output
    assert normalize_tool_output("write", json.dumps(output)) == json.dumps(output)
