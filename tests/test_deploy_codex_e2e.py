"""Optional live Codex -> workspace handoff -> artifact sealing test.

Gated behind ``CODEX_DEPLOY_E2E=1`` because it invokes the real Codex CLI and
may use network/model spend. Cloud deployment itself is deliberately not a
Codex responsibility; this test stops at the immutable artifact boundary.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest

from aios_core.deploy.handoff_artifacts import create_uploaded_artifact_from_handoff
from aios_core.deploy.worktree_handoff import WorktreeRegistry
from aios_core.tools.codex_job import CodexJobManager

pytestmark = pytest.mark.skipif(
    not (shutil.which("codex") and os.getenv("CODEX_DEPLOY_E2E")),
    reason="set CODEX_DEPLOY_E2E=1 for the live Codex handoff/artifact e2e",
)

_TASK = (
    "Build a frontend whose index.html contains exactly the visible text "
    "HELLO-FROM-CODEX-E2E. Keep the existing app ID and deployment manifest."
)


class _Cloud:
    def upload_artifact(self, archive) -> str:
        assert archive.manifest.app_id == "app_codexe2e"
        assert archive.path.is_file()
        return "art_codexe2e"


def test_codex_prepares_source_and_main_seals_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    apps_root = workspace / "apps"
    app = apps_root / "app_codexe2e"
    app.mkdir(parents=True)
    (app / ".aios-app.json").write_text(
        json.dumps({"version": 1, "app_id": "app_codexe2e", "name": "E2E"}) + "\n"
    )
    (app / "aios.deploy.yaml").write_text(
        "version: 1\napp_id: app_codexe2e\nfrontend:\n  source: frontend\n"
    )
    registry = WorktreeRegistry(workspace / ".aios" / "worktrees", apps_root=apps_root)
    monkeypatch.setattr(
        "aios_core.tools.codex_job.resolve_chat_files_path", lambda path: app
    )
    manager = CodexJobManager(worktree_registry=registry)

    started = manager.start(_TASK, path=str(app), enable_deploy=True)
    assert "job_id" in started, started
    deadline = time.time() + 360
    final = manager.poll(started["job_id"])
    while time.time() < deadline and final["status"] in {
        "running",
        "awaiting_input",
    }:
        final = manager.poll(started["job_id"], cursor=final["cursor"], wait=15)

    assert final["status"] == "done", final
    handoff = final["workspace_handoff"]
    receipt = create_uploaded_artifact_from_handoff(
        registry=registry,
        cloud=_Cloud(),  # type: ignore[arg-type]
        handoff_id=handoff["handoff_id"],
    )

    assert receipt.artifact_id == "art_codexe2e"
    assert receipt.cleanup_status == "removed"
