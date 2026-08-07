from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aios_core.apps.coordinator import AppCoordinator
from aios_core.apps.mcp import _tool_prefix
from aios_core.apps.models import McpServerSpec
from aios_core.apps.registry import AppLifecycleError
from aios_core.apps.runtime import RuntimeResult, StopResult
from aios_core.apps.service import AppService
from aios_core.skills import load_skills
from aios_core.tools.skill import read_skill
from aios_core.workspace import get_runtime_paths
from server import apps as apps_api
from server.auth import require_local_token


class FakeRuntime:
    def __init__(self) -> None:
        self.prepared: list[tuple[object, object, bool]] = []
        self.executed: list[tuple[object, object, tuple[object, ...], bool]] = []
        self.stopped: list[str] = []
        self.cleared: list[str] = []
        self.stop_ok = True

    def available(self) -> bool:
        return True

    def prepare(self, app, snapshot, *, network_approved=False) -> RuntimeResult:
        self.prepared.append((app, snapshot, network_approved))
        return RuntimeResult(True, 0, "", "", False, runtime_path="/runtime")

    def run_executable(
        self,
        app,
        executable,
        args=(),
        *,
        network_approved=False,
    ) -> RuntimeResult:
        self.executed.append((app, executable, tuple(args), network_approved))
        return RuntimeResult(True, 0, "ok", "", False)

    def stop_app(self, app_id) -> StopResult:
        self.stopped.append(str(app_id))
        return StopResult(
            self.stop_ok,
            1 if self.stop_ok else 0,
            "" if self.stop_ok else "stop failed",
        )

    def clear_data(self, app_id) -> bool:
        self.cleared.append(str(app_id))
        return True


@pytest.fixture
def apps_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "aios"
    monkeypatch.setenv("AIOS_HOME", str(root))
    monkeypatch.delenv("AIOS_STATE_DIR", raising=False)
    monkeypatch.delenv("AIOS_SKILLS_DIR", raising=False)
    monkeypatch.delenv("AIOS_WORKSPACE_DIR", raising=False)
    paths = get_runtime_paths()
    service = AppService(
        applications_dir=paths.applications,
        state_dir=paths.state,
        db_path=paths.database,
    )
    runtime = FakeRuntime()
    return paths, AppCoordinator(service=service, runtime=runtime), runtime


def _write_manifest(app_root: Path, **overrides) -> None:
    manifest = {
        "schemaVersion": 1,
        "name": "Demo",
        "description": "A test App",
        "version": "1.0.0",
        "skills": [],
        "mcpServers": [],
        "executables": [],
        "prepare": [],
        "runtime": {
            "network": False,
            "persistentData": False,
            "memoryMb": 256,
            "cpus": 0.5,
            "maxProcesses": 16,
        },
    }
    manifest.update(overrides)
    (app_root / "app.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def _enable_skill_app(coordinator: AppCoordinator, paths) -> str:
    created = coordinator.create("demo", name="Demo")
    app_id = created["app"]["id"]
    app_root = paths.applications / "demo"
    skill = app_root / "skills" / "guide" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: guide\ndescription: Stable instructions.\n---\n\n# Guide\n\nold active instructions\n",
        encoding="utf-8",
    )
    _write_manifest(
        app_root,
        skills=[{"id": "guide", "path": "skills/guide/SKILL.md"}],
    )
    coordinator.validate(app_id)
    coordinator.prepare(app_id)
    coordinator.enable(app_id)
    return app_id


def test_enabled_app_skills_use_the_active_immutable_snapshot(apps_layout) -> None:
    paths, coordinator, _runtime = apps_layout
    app_id = _enable_skill_app(coordinator, paths)

    listed = load_skills()
    loaded = read_skill("demo/guide")
    assert [skill["name"] for skill in listed] == ["demo/guide"]
    assert "old active instructions" in loaded["instructions"]
    assert loaded["skill"]["file"] == "app://demo/guide"

    app_root = paths.applications / "demo"
    (app_root / "skills" / "guide" / "SKILL.md").write_text(
        "# Guide\n\nnew editable instructions\n",
        encoding="utf-8",
    )
    _write_manifest(
        app_root,
        version="2.0.0",
        skills=[{"id": "guide", "path": "skills/guide/SKILL.md"}],
    )
    updated = coordinator.validate(app_id)["app"]

    assert updated["status"] == "update_pending"
    assert coordinator.inspect(app_id)["activeManifest"]["version"] == "1.0.0"
    assert "old active instructions" in read_skill("demo/guide")["instructions"]
    assert "new editable instructions" not in read_skill("demo/guide")["instructions"]

    coordinator.disable(app_id)
    assert all(skill["name"] != "demo/guide" for skill in load_skills())


def test_app_skill_reads_and_prompt_metadata_are_bounded(apps_layout) -> None:
    paths, coordinator, _runtime = apps_layout
    created = coordinator.create("bounded", name="Bounded")
    app_id = created["app"]["id"]
    app_root = paths.applications / "bounded"
    skill = app_root / "skills" / "guide" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\ndescription: {'d' * 3000}\n---\n\n# {'t' * 500}\n\n{'x' * 150_000}",
        encoding="utf-8",
    )
    _write_manifest(
        app_root,
        skills=[{"id": "guide", "path": "skills/guide/SKILL.md"}],
    )
    coordinator.validate(app_id)
    coordinator.prepare(app_id)
    coordinator.enable(app_id)

    listed = next(skill for skill in load_skills() if skill["name"] == "bounded/guide")
    loaded = read_skill("bounded/guide")
    assert len(listed["title"]) <= 360
    assert len(listed["summary"]) == 2000
    assert len(loaded["instructions"]) == 100_000
    assert loaded["truncated"] is True

    skill.write_text("x" * (513 * 1024), encoding="utf-8")
    with pytest.raises(ValueError, match="skill guide cannot exceed"):
        coordinator.validate(app_id)
    assert any(skill["name"] == "bounded/guide" for skill in load_skills())


def test_tampered_active_skill_snapshot_is_not_loaded(apps_layout) -> None:
    paths, coordinator, _runtime = apps_layout
    app_id = _enable_skill_app(coordinator, paths)
    app = coordinator.registry.require(app_id)
    snapshot_root = coordinator.service.snapshot_path(app, app.active_hash)
    skill_path = snapshot_root / "skills" / "guide" / "SKILL.md"
    skill_path.chmod(0o644)
    skill_path.write_text("# Tampered\n\nuntrusted instructions", encoding="utf-8")

    assert all(skill["name"] != "demo/guide" for skill in load_skills())
    assert read_skill("demo/guide")["error"] == "skill not found: demo/guide"


def test_mcp_prefixes_remain_unique_after_normalization() -> None:
    server = McpServerSpec(id="tools", cwd=".", command=("python", "server.py"))
    hyphenated = type("App", (), {"id": "app-one", "slug": "foo-bar"})()
    underscored = type("App", (), {"id": "app-two", "slug": "foo_bar"})()

    assert _tool_prefix(hyphenated, server) != _tool_prefix(underscored, server)


def test_failed_validation_keeps_the_previous_enabled_version(apps_layout) -> None:
    paths, coordinator, _runtime = apps_layout
    app_id = _enable_skill_app(coordinator, paths)
    app_root = paths.applications / "demo"
    _write_manifest(
        app_root,
        version="2.0.0",
        executables=[{"id": "bad", "cwd": ".", "command": "python bad.py"}],
    )

    with pytest.raises(ValueError, match="command must be an array"):
        coordinator.validate(app_id)

    app = coordinator.registry.require(app_id)
    assert app.enabled is True
    assert app.active_hash is not None
    assert app.active_hash == app.validated_hash
    assert app.last_error
    assert "old active instructions" in read_skill("demo/guide")["instructions"]


def test_run_uses_the_active_manifest_during_an_update(apps_layout) -> None:
    paths, coordinator, runtime = apps_layout
    created = coordinator.create("runner", name="Runner")
    app_id = created["app"]["id"]
    app_root = paths.applications / "runner"
    _write_manifest(
        app_root,
        name="Runner",
        executables=[{"id": "task", "cwd": ".", "command": ["python", "old.py"]}],
    )
    coordinator.validate(app_id)
    coordinator.prepare(app_id)
    coordinator.enable(app_id)

    _write_manifest(
        app_root,
        name="Runner",
        version="2.0.0",
        executables=[{"id": "task", "cwd": ".", "command": ["python", "new.py"]}],
    )
    coordinator.validate(app_id)
    result = coordinator.run(app_id, "task", ["--safe"])

    assert result["runtime"]["ok"] is True
    assert runtime.executed[-1][1].command == ("python", "old.py")
    assert runtime.executed[-1][2] == ("--safe",)


def test_disable_keeps_app_enabled_when_containers_cannot_stop(apps_layout) -> None:
    paths, coordinator, runtime = apps_layout
    created = coordinator.create("runner", name="Runner")
    app_id = created["app"]["id"]
    _write_manifest(
        paths.applications / "runner",
        name="Runner",
        executables=[{"id": "task", "cwd": ".", "command": ["python", "run.py"]}],
    )
    coordinator.validate(app_id)
    coordinator.prepare(app_id)
    coordinator.enable(app_id)
    runtime.stop_ok = False

    with pytest.raises(AppLifecycleError, match="stop failed"):
        coordinator.disable(app_id)

    assert coordinator.registry.require(app_id).enabled is True


def test_reset_data_uses_the_runtime_recovery_path(apps_layout) -> None:
    _paths, coordinator, runtime = apps_layout
    created = coordinator.create("resettable", name="Resettable")
    app_id = created["app"]["id"]

    result = coordinator.reset_data(app_id)

    assert result["dataReset"] is True
    assert runtime.cleared == [app_id]


def test_apps_api_uses_list_envelope_and_direct_action_summary(monkeypatch) -> None:
    summary = {
        "id": "app-1",
        "slug": "demo",
        "name": "Demo",
        "description": "",
        "version": "1.0.0",
        "origin": "user",
        "rootPath": "applications/demo",
        "status": "ready",
        "enabled": False,
        "validatedHash": "a" * 64,
        "preparedHash": "a" * 64,
        "activeHash": None,
        "networkApproved": False,
        "components": {"skills": 1, "mcpServers": 0, "executables": 0},
        "lastError": None,
        "createdAt": 1,
        "updatedAt": 1,
    }

    class FakeCoordinator:
        def list(self):
            return {"apps": [summary]}

        def enable(self, _app_id):
            return {"app": {**summary, "enabled": True, "status": "enabled"}}

    monkeypatch.setattr(apps_api, "_coordinator", lambda: FakeCoordinator())
    api = FastAPI()
    api.include_router(apps_api.router)
    api.dependency_overrides[require_local_token] = lambda: None

    with TestClient(api) as client:
        listed = client.get("/apps")
        enabled = client.post("/apps/app-1/enable")

    assert listed.status_code == 200
    assert listed.json() == {"apps": [summary]}
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert "app" not in enabled.json()
