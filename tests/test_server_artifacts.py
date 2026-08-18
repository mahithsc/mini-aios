from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from server import server


def test_artifact_endpoint_rejects_symlinked_chat_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    artifacts_root = tmp_path / "artifacts" / "chat-1"
    artifacts_root.parent.mkdir()
    artifacts_root.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(
        server,
        "get_chat_artifacts_dir",
        lambda _chat_id: artifacts_root,
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(server.get_session_artifact_file("chat-1", "secret.txt"))

    assert raised.value.status_code == 404
