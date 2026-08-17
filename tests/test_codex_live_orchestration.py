"""Opt-in live smoke test for the real Codex app-server lifecycle."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from aios_core.tools.codex_job import CodexJobManager
from aios_core.tools.codex_run_store import CodexRunStore


pytestmark = pytest.mark.skipif(
    os.getenv("AIOS_RUN_LIVE_CODEX_E2E") != "1",
    reason="set AIOS_RUN_LIVE_CODEX_E2E=1 to invoke the real Codex CLI",
)


def test_real_codex_edits_and_completes_durable_run(tmp_path: Path) -> None:
    manager = CodexJobManager(CodexRunStore(str(tmp_path / "codex.db")))
    started = manager.start(
        "Create a file named lifecycle-proof.txt containing exactly: recovered and verified",
        path=str(tmp_path),
        session_id="live-chat",
        parent_run_id="live-parent",
    )
    assert "error" not in started

    deadline = time.monotonic() + 300
    result = manager.poll(started["job_id"])
    while result.get("status") not in {"done", "error", "cancelled"}:
        if time.monotonic() >= deadline:
            manager.stop(started["job_id"])
            pytest.fail("real Codex job did not complete within five minutes")
        time.sleep(0.25)
        result = manager.poll(started["job_id"])

    assert result["status"] == "done", result.get("error")
    assert (tmp_path / "lifecycle-proof.txt").read_text().strip() == (
        "recovered and verified"
    )
    record = manager.store.get(started["job_id"])
    assert record is not None
    assert record["parent_run_id"] == "live-parent"
    assert manager.store.pending_signals() == [(started["job_id"], "done")]
