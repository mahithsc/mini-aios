from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aios_core.apps.paths import (
    AppHostExecutionDenied,
    ensure_host_execution_allowed,
    protected_app_roots,
)
from aios_core.tools import execution_sandbox
from aios_core.tools.codex import codex
from aios_core.tools.execution_sandbox import ExecutionSandboxUnavailable
from aios_core.tools.filesystem import write
from aios_core.tools.processes import ProcessManager
from aios_core.tools.shell import bash
from aios_core.workspace import get_runtime_paths


@pytest.fixture
def managed_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "aios"
    monkeypatch.setenv("AIOS_HOME", str(root))
    monkeypatch.delenv("AIOS_STATE_DIR", raising=False)
    monkeypatch.delenv("AIOS_SKILLS_DIR", raising=False)
    monkeypatch.delenv("AIOS_WORKSPACE_DIR", raising=False)
    paths = get_runtime_paths()
    app_root = paths.applications / "demo"
    app_root.mkdir(parents=True)
    (app_root / "app.json").write_text("{}", encoding="utf-8")
    return paths, app_root


def test_managed_app_roots_are_discovered_before_registration(managed_app) -> None:
    paths, app_root = managed_app

    assert protected_app_roots(paths) == (app_root.resolve(),)
    with pytest.raises(AppHostExecutionDenied, match="isolated App runtime"):
        ensure_host_execution_allowed(app_root / "scripts", paths=paths)


def test_shell_and_process_cwd_reject_managed_app(managed_app) -> None:
    _paths, app_root = managed_app

    assert bash("python script.py", cwd=str(app_root)).startswith(
        "error: managed App code cannot run"
    )
    result = ProcessManager().spawn(cwd=str(app_root))
    assert result["error"].startswith("managed App code cannot run")
    assert codex("make a change", path=str(app_root)).startswith(
        "error: managed App code cannot run"
    )


def test_native_profile_denies_reads_and_writes_for_managed_app(managed_app) -> None:
    paths, app_root = managed_app

    profile = execution_sandbox._sandbox_profile(paths)

    escaped = str(app_root.resolve())
    assert f'(deny file-read* (subpath "{escaped}"))' in profile
    assert f'(deny file-write* (subpath "{escaped}"))' in profile
    assert f'(deny file-map-executable (subpath "{escaped}"))' in profile
    assert f'(deny process-exec (subpath "{escaped}"))' in profile
    assert "/[^/]+/app\\.json$" in profile


def test_native_sandbox_blocks_host_exec_from_managed_app(managed_app) -> None:
    paths, app_root = managed_app
    if execution_sandbox._native_sandbox_command() is None:
        pytest.skip("native sandbox unavailable")
    executable = app_root / "run"
    shutil.copyfile("/usr/bin/true", executable)
    executable.chmod(0o755)

    output = bash("./demo/run; printf 'child-exit:%s' $?", cwd=str(paths.applications))

    assert "child-exit:0" not in output


def test_native_sandbox_cannot_create_app_manifest_in_shell(managed_app) -> None:
    paths, _app_root = managed_app
    if execution_sandbox._native_sandbox_command() is None:
        pytest.skip("native sandbox unavailable")

    output = bash(
        "mkdir -p late-app; printf '{}' > late-app/app.json; test ! -e late-app/app.json",
        cwd=str(paths.applications),
    )

    assert "exit code" not in output
    assert not (paths.applications / "late-app" / "app.json").exists()


def test_unwrapped_execution_is_not_an_app_isolation_bypass(
    managed_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _app_root = managed_app
    monkeypatch.setenv("AIOS_ALLOW_UNSANDBOXED_EXECUTION", "true")
    monkeypatch.setattr(execution_sandbox, "_native_sandbox_command", lambda: None)

    with pytest.raises(ExecutionSandboxUnavailable, match="sandbox is unavailable"):
        execution_sandbox.sandboxed_command(["/usr/bin/true"], paths=paths)


def test_allow_unwrapped_parameter_cannot_bypass_managed_apps(
    managed_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _app_root = managed_app
    monkeypatch.delenv("AIOS_ALLOW_UNSANDBOXED_EXECUTION", raising=False)
    monkeypatch.setattr(execution_sandbox, "_native_sandbox_command", lambda: None)

    with pytest.raises(ExecutionSandboxUnavailable, match="managed Apps exist"):
        execution_sandbox.sandboxed_command(
            ["codex", "exec"],
            paths=paths,
            allow_unwrapped=True,
        )


def test_process_session_closes_when_apps_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "aios"
    monkeypatch.setenv("AIOS_HOME", str(root))
    monkeypatch.delenv("AIOS_STATE_DIR", raising=False)
    monkeypatch.delenv("AIOS_SKILLS_DIR", raising=False)
    monkeypatch.delenv("AIOS_WORKSPACE_DIR", raising=False)
    paths = get_runtime_paths()
    paths.applications.mkdir(parents=True)
    monkeypatch.setattr(
        "aios_core.tools.processes.ProcessSession.start",
        lambda self: self,
    )
    monkeypatch.setattr(
        "aios_core.tools.processes.ProcessSession.close",
        lambda self: None,
    )
    manager = ProcessManager()
    spawned = manager.spawn()
    app_root = paths.applications / "late-app"
    app_root.mkdir()
    (app_root / "app.json").write_text("{}", encoding="utf-8")

    result = manager.send(spawned["process_id"], command="python main.py")

    assert result["error"].startswith("managed Apps changed")
    assert manager.list() == []


def test_file_tool_quiesces_processes_before_creating_app_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "aios"
    monkeypatch.setenv("AIOS_HOME", str(root))
    monkeypatch.delenv("AIOS_STATE_DIR", raising=False)
    monkeypatch.delenv("AIOS_SKILLS_DIR", raising=False)
    monkeypatch.delenv("AIOS_WORKSPACE_DIR", raising=False)
    calls: list[bool] = []
    monkeypatch.setattr(
        "aios_core.tools.processes.close_all_processes",
        lambda: calls.append(True),
    )

    assert write("late-app/app.json", "{}").startswith("ok:")
    assert calls == [True]
    assert write("notes.txt", "not an App").startswith("ok:")
    assert calls == [True]
