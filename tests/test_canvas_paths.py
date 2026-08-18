from __future__ import annotations

from pathlib import Path

from aios_core.agent.tools import canvas


def test_canvas_infers_url_from_canonical_artifact_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / ".mini-aios"
    monkeypatch.setattr(canvas, "ensure_workspace_dir", lambda: data_dir)
    monkeypatch.setattr(canvas, "DEFAULT_SERVER_BASE_URL", "http://localhost:8765")

    url = canvas._infer_served_url_from_file_path(
        "artifacts/chat-1/report/index.html"
    )

    assert url == (
        "http://localhost:8765/session-artifacts/chat-1/report/index.html"
    )


def test_canvas_keeps_legacy_nested_artifact_path_compatibility(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / ".mini-aios"
    monkeypatch.setattr(canvas, "ensure_workspace_dir", lambda: data_dir)
    monkeypatch.setattr(canvas, "DEFAULT_SERVER_BASE_URL", "http://localhost:8765")

    url = canvas._infer_served_url_from_file_path(
        "session/chat-1/artifacts/report/index.html"
    )

    assert url == (
        "http://localhost:8765/session-artifacts/chat-1/report/index.html"
    )

    workspace_prefixed_url = canvas._infer_served_url_from_file_path(
        "workspace/session/chat-1/artifacts/report/index.html"
    )
    assert workspace_prefixed_url == url
