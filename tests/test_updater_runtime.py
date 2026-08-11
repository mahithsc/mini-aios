from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aios_core import runtime_control
from aios_core.runtime_control import RuntimeControl, RuntimeDrainingError
from server import updater


def test_runtime_control_persists_drain_across_instances(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "update-drain.json"
    first = RuntimeControl(state_path)
    second = RuntimeControl(state_path)

    assert first.snapshot().draining is False
    first.request_drain("release test")
    assert second.snapshot().draining is True
    try:
        second.ensure_accepting_work()
    except RuntimeDrainingError:
        pass
    else:
        raise AssertionError("drained runtime accepted new work")

    second.resume()
    assert first.snapshot().draining is False


def test_updater_api_auth_drain_ready_and_resume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("skills", "session", "runs"):
        (workspace / name).mkdir()
    database = workspace / "aios.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")

    control = RuntimeControl(workspace / "update-drain.json")
    monkeypatch.setattr(runtime_control, "_runtime_control", control)
    monkeypatch.setattr(
        updater,
        "ensure_workspace_dir",
        lambda: workspace,
    )
    monkeypatch.setattr(
        updater,
        "get_db_connection",
        lambda: sqlite3.connect(database),
    )
    monkeypatch.setattr(updater, "_active_runs", lambda: 2)
    monkeypatch.setenv("AIOS_UPDATER_TOKEN", "test-updater-token")
    monkeypatch.setenv("AIOS_RELEASE_ID", "test-release")
    monkeypatch.setenv("AIOS_RELEASE_SEQUENCE", "7")

    app = FastAPI()
    app.include_router(updater.router)
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-updater-token"}

    assert client.get("/internal/updater/ready").status_code == 401

    ready = client.get("/internal/updater/ready", headers=headers)
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["releaseId"] == "test-release"
    assert ready.json()["sequence"] == 7

    drained = client.post("/internal/updater/drain", headers=headers)
    assert drained.status_code == 200
    assert drained.json()["draining"] is True
    assert drained.json()["activeRuns"] == 2

    resumed = client.post("/internal/updater/resume", headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["draining"] is False
