from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aios_core.apps.manifest import manifest_to_dict
from aios_core.apps.models import (
    AppManifest,
    ExecutableSpec,
    McpServerSpec,
    RuntimeSpec,
    SkillSpec,
    Snapshot,
)
from aios_core.apps.runtime import (
    AppRuntime,
    DockerUnavailableError,
    RuntimeConfigurationError,
)
from aios_core.apps.service import AppService


class FakeControlRun:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(
        self, command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        if command[1] == "version":
            return subprocess.CompletedProcess(
                command, 0 if self.available else 1, "26.0", ""
            )
        if command[1] == "ps":
            return subprocess.CompletedProcess(
                command, 0, "container-one\ncontainer-two\n", ""
            )
        if command[1] == "stop":
            return subprocess.CompletedProcess(
                command, 0, "container-one\ncontainer-two\n", ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")


class FakeImageBuildControl(FakeControlRun):
    def __init__(self) -> None:
        super().__init__()
        self.built = False

    def __call__(
        self, command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        if command[1] == "version":
            return subprocess.CompletedProcess(command, 0, "26.0", "")
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                command, 0 if self.built else 1, "", "missing"
            )
        if command[1] == "build":
            self.built = True
            return subprocess.CompletedProcess(command, 0, "built", "")
        return subprocess.CompletedProcess(command, 0, "", "")


class FakeProcess:
    def __init__(
        self,
        command: list[str],
        kwargs: dict[str, Any],
        *,
        stdout: bytes,
        stderr: bytes,
        exit_code: int,
    ) -> None:
        self.command = command
        self.kwargs = kwargs
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.exit_code = exit_code
        self.killed = False

    def wait(self, timeout: int) -> int:
        return self.exit_code

    def kill(self) -> None:
        self.killed = True


class FakePopen:
    def __init__(
        self,
        *,
        stdout: bytes = b"done",
        stderr: bytes = b"",
        exit_code: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.processes: list[FakeProcess] = []

    def __call__(self, command: list[str], **kwargs: Any) -> FakeProcess:
        process = FakeProcess(
            command,
            kwargs,
            stdout=self.stdout,
            stderr=self.stderr,
            exit_code=self.exit_code,
        )
        self.processes.append(process)
        return process


class TimeoutProcess(FakeProcess):
    def __init__(self, command: list[str], kwargs: dict[str, Any]) -> None:
        super().__init__(command, kwargs, stdout=b"partial", stderr=b"", exit_code=137)
        self.wait_count = 0

    def wait(self, timeout: int) -> int:
        self.wait_count += 1
        if self.wait_count == 1:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.exit_code


class TimeoutPopen:
    def __init__(self) -> None:
        self.processes: list[TimeoutProcess] = []

    def __call__(self, command: list[str], **kwargs: Any) -> TimeoutProcess:
        process = TimeoutProcess(command, kwargs)
        self.processes.append(process)
        return process


@pytest.fixture
def snapshot(tmp_path: Path) -> Snapshot:
    path = tmp_path / "apps-state" / "snapshots" / "app-1" / "hash-1" / "app"
    path.mkdir(parents=True)
    (path / "app.json").write_text("{}", encoding="utf-8")
    return Snapshot(content_hash="hash-1", path=path, file_count=1, size_bytes=2)


def app_with(manifest: AppManifest) -> SimpleNamespace:
    return SimpleNamespace(id="app-1", active_hash="hash-1", manifest=manifest)


def write_snapshot_manifest(snapshot: Snapshot, manifest: AppManifest) -> None:
    (snapshot.path / "app.json").write_text(
        json.dumps(manifest_to_dict(manifest)),
        encoding="utf-8",
    )


def make_runtime(
    snapshot: Snapshot,
    *,
    control: FakeControlRun | None = None,
    popen: FakePopen | None = None,
    output_bytes: int = 64 * 1024,
) -> tuple[AppRuntime, FakeControlRun, FakePopen]:
    control = control or FakeControlRun()
    popen = popen or FakePopen()
    state_root = snapshot.path.parents[3]
    return (
        AppRuntime(
            state_root,
            control_run=control,
            popen=popen,
            output_bytes=output_bytes,
        ),
        control,
        popen,
    )


def test_skill_only_app_prepares_without_docker(snapshot: Snapshot) -> None:
    class DockerMustNotRun:
        def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("skill-only Apps must not probe Docker")

    manifest = AppManifest(
        schema_version=1,
        name="Skill App",
        description="",
        version="1.0.0",
        skills=(SkillSpec(id="guide", path="skills/guide/SKILL.md"),),
    )
    write_snapshot_manifest(snapshot, manifest)
    runtime = AppRuntime(snapshot.path.parents[3], control_run=DockerMustNotRun())

    result = runtime.prepare(app_with(manifest), snapshot)

    assert result.ok is True
    assert result.exit_code == 0
    assert result.timed_out is False
    assert Path(result.runtime_path or "").is_dir()


def test_prepare_rejects_a_tampered_registry_snapshot(tmp_path: Path) -> None:
    service = AppService(
        applications_dir=tmp_path / "applications",
        state_dir=tmp_path / "state",
        db_path=tmp_path / "state" / "aios.db",
    )
    app = service.create_app("tamper")
    validated = service.validate(app.id)
    manifest_path = validated.snapshot.path / "app.json"
    validated.snapshot.path.chmod(0o755)
    manifest_path.chmod(0o644)
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )
    runtime = AppRuntime(tmp_path / "state" / "apps")

    with pytest.raises(RuntimeConfigurationError, match="registered hash"):
        runtime.prepare(validated.app, validated.snapshot)


def test_executable_fails_closed_when_docker_is_unavailable(snapshot: Snapshot) -> None:
    executable = ExecutableSpec(id="report", cwd=".", command=("python", "report.py"))
    app = app_with(AppManifest(1, "App", "", "1", executables=(executable,)))
    write_snapshot_manifest(snapshot, app.manifest)
    popen = FakePopen()
    runtime, _control, _popen = make_runtime(
        snapshot,
        control=FakeControlRun(available=False),
        popen=popen,
    )

    with pytest.raises(DockerUnavailableError, match="cannot run on the host"):
        runtime.prepare(app, snapshot)

    assert popen.processes == []


def test_first_executable_prepare_builds_trusted_runtime_image(
    snapshot: Snapshot,
    tmp_path: Path,
) -> None:
    executable = ExecutableSpec(id="report", cwd=".", command=("python", "report.py"))
    manifest = AppManifest(1, "App", "", "1", executables=(executable,))
    app = app_with(manifest)
    write_snapshot_manifest(snapshot, manifest)
    build_context = tmp_path / "runtime-image"
    build_context.mkdir()
    (build_context / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    control = FakeImageBuildControl()
    runtime = AppRuntime(
        snapshot.path.parents[3],
        build_context=build_context,
        control_run=control,
    )

    assert runtime.prepare(app, snapshot).ok is True
    assert runtime.prepare(app, snapshot).ok is True

    builds = [command for command, _kwargs in control.calls if command[1] == "build"]
    assert builds == [
        [
            "docker",
            "build",
            "--tag",
            "aios-app-runtime:v1",
            str(build_context),
        ]
    ]
    assert all(kwargs["shell"] is False for _command, kwargs in control.calls)


def test_executable_uses_constrained_container_and_bounded_output(
    snapshot: Snapshot,
) -> None:
    executable = ExecutableSpec(
        id="report",
        cwd="scripts",
        command=("python", "report.py"),
        timeout_seconds=30,
    )
    manifest = AppManifest(
        1,
        "App",
        "",
        "1",
        executables=(executable,),
        runtime=RuntimeSpec(memory_mb=256, cpus=0.5, max_processes=24),
    )
    app = app_with(manifest)
    write_snapshot_manifest(snapshot, manifest)
    runtime, _control, popen = make_runtime(
        snapshot,
        popen=FakePopen(stdout=b"x" * 3000),
        output_bytes=1024,
    )
    assert runtime.prepare(app, snapshot).ok

    result = runtime.run_executable(app, executable, ["--format", "json"])

    command = popen.processes[-1].command
    assert popen.processes[-1].kwargs["shell"] is False
    assert command[:5] == ["docker", "run", "--rm", "--pull", "never"]
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--memory") + 1] == "256m"
    assert command[command.index("--memory-swap") + 1] == "256m"
    assert command[command.index("--cpus") + 1] == "0.5"
    assert command[command.index("--pids-limit") + 1] == "24"
    assert command[command.index("--workdir") + 1] == "/app/scripts"
    assert command[-5:] == [
        "aios-app-runtime:v1",
        "python",
        "report.py",
        "--format",
        "json",
    ]
    mounts = [
        command[index + 1] for index, part in enumerate(command) if part == "--mount"
    ]
    assert len(mounts) == 2
    assert any("dst=/app,readonly" in mount for mount in mounts)
    assert any("dst=/runtime,readonly" in mount for mount in mounts)
    assert all(
        "workspace" not in mount and "docker.sock" not in mount for mount in mounts
    )
    assert result.ok is True
    assert result.truncated is True
    assert result.stdout.endswith("[output truncated]")
    assert len(result.stdout) < 1200


def test_persistent_data_is_bounded_and_mcp_mount_is_read_only(
    snapshot: Snapshot,
) -> None:
    executable = ExecutableSpec(id="write", cwd=".", command=("python", "write.py"))
    server = McpServerSpec(id="tools", cwd=".", command=("python", "server.py"))
    manifest = AppManifest(
        1,
        "App",
        "",
        "1",
        mcp_servers=(server,),
        executables=(executable,),
        runtime=RuntimeSpec(persistent_data=True),
    )
    app = app_with(manifest)
    write_snapshot_manifest(snapshot, manifest)
    runtime, _control, _popen = make_runtime(snapshot)
    assert runtime.prepare(app, snapshot).ok

    assert runtime.run_executable(app, executable).ok
    assert runtime.run_executable(app, executable).ok
    first = runtime.mcp_server_parameters(app, server)
    second = runtime.mcp_server_parameters(app, server)

    first_args = list(first["args"])
    second_args = list(second["args"])
    first_name = first_args[first_args.index("--name") + 1]
    second_name = second_args[second_args.index("--name") + 1]
    assert first_name != second_name
    assert "-i" in first_args
    mounts = [
        first_args[index + 1]
        for index, part in enumerate(first_args)
        if part == "--mount"
    ]
    assert any("dst=/data,readonly" in mount for mount in mounts)
    assert first["command"] == "docker"
    assert set(first["env"]) == {"PATH"}
    assert first["cwd"] is None


def test_verified_mcp_snapshot_is_reused_without_rehashing_each_server(
    snapshot: Snapshot,
) -> None:
    server = McpServerSpec(id="tools", cwd=".", command=("python", "server.py"))
    manifest = AppManifest(1, "App", "", "1", mcp_servers=(server,))
    app = app_with(manifest)
    write_snapshot_manifest(snapshot, manifest)
    runtime, _control, _popen = make_runtime(snapshot)
    assert runtime.prepare(app, snapshot).ok

    def unexpected_rehash(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("verified App snapshot should be reused")

    runtime._verify_snapshot_content = unexpected_rehash
    parameters = runtime.mcp_server_parameters(
        app,
        server,
        verified_snapshot=snapshot,
    )

    assert parameters["command"] == "docker"


def test_executable_refuses_persistent_data_over_the_host_limit(
    snapshot: Snapshot,
) -> None:
    executable = ExecutableSpec(id="write", cwd=".", command=("python", "write.py"))
    manifest = AppManifest(
        1,
        "App",
        "",
        "1",
        executables=(executable,),
        runtime=RuntimeSpec(persistent_data=True),
    )
    app = app_with(manifest)
    write_snapshot_manifest(snapshot, manifest)
    runtime, _control, popen = make_runtime(snapshot)
    runtime.data_storage_bytes = 1024 * 1024
    assert runtime.prepare(app, snapshot).ok
    data_path = runtime.data_root / "app-1"
    data_path.mkdir(parents=True)
    (data_path / "too-large.bin").write_bytes(b"x" * (1024 * 1024 + 1))

    with pytest.raises(RuntimeConfigurationError, match="persistent App data exceeded"):
        runtime.run_executable(app, executable)

    assert popen.processes == []
    assert runtime.clear_data(app.id) is True
    assert runtime.run_executable(app, executable).ok


def test_executable_refuses_persistent_data_over_the_entry_limit(
    snapshot: Snapshot,
) -> None:
    executable = ExecutableSpec(id="write", cwd=".", command=("python", "write.py"))
    manifest = AppManifest(
        1,
        "App",
        "",
        "1",
        executables=(executable,),
        runtime=RuntimeSpec(persistent_data=True),
    )
    app = app_with(manifest)
    write_snapshot_manifest(snapshot, manifest)
    runtime, _control, popen = make_runtime(snapshot)
    runtime.data_storage_files = 2
    assert runtime.prepare(app, snapshot).ok
    data_path = runtime.data_root / "app-1"
    data_path.mkdir(parents=True)
    for index in range(3):
        (data_path / f"{index}.txt").touch()

    with pytest.raises(RuntimeConfigurationError, match="entry limit"):
        runtime.run_executable(app, executable)

    assert popen.processes == []


def test_network_requires_explicit_approval(snapshot: Snapshot) -> None:
    executable = ExecutableSpec(id="fetch", cwd=".", command=("python", "fetch.py"))
    manifest = AppManifest(
        1,
        "App",
        "",
        "1",
        executables=(executable,),
        runtime=RuntimeSpec(network=True),
    )
    app = app_with(manifest)
    write_snapshot_manifest(snapshot, manifest)
    runtime, _control, popen = make_runtime(snapshot)
    assert runtime.prepare(app, snapshot).ok

    with pytest.raises(RuntimeConfigurationError, match="without approval"):
        runtime.run_executable(app, executable)

    result = runtime.run_executable(app, executable, network_approved=True)
    assert result.ok
    assert (
        popen.processes[-1].command[popen.processes[-1].command.index("--network") + 1]
        == "bridge"
    )


def test_active_snapshot_policy_wins_over_a_newer_record_manifest(
    snapshot: Snapshot,
) -> None:
    executable = ExecutableSpec(id="run", cwd=".", command=("python", "run.py"))
    active_manifest = AppManifest(
        1,
        "Old",
        "",
        "1",
        executables=(executable,),
        runtime=RuntimeSpec(network=False, memory_mb=128),
    )
    newer_manifest = AppManifest(
        1,
        "New",
        "",
        "2",
        executables=(executable,),
        runtime=RuntimeSpec(network=True, memory_mb=1024),
    )
    write_snapshot_manifest(snapshot, active_manifest)
    app = app_with(newer_manifest)
    runtime, _control, popen = make_runtime(snapshot)
    assert runtime.prepare(app, snapshot).ok

    result = runtime.run_executable(app, executable)

    assert result.ok
    command = popen.processes[-1].command
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--memory") + 1] == "128m"


def test_stop_app_only_targets_managed_labeled_containers(snapshot: Snapshot) -> None:
    runtime, control, _popen = make_runtime(snapshot)

    result = runtime.stop_app("app-1")

    assert result.ok is True
    assert result.stopped == 2
    ps_command = next(
        command for command, _kwargs in control.calls if command[1] == "ps"
    )
    assert "label=aios.managed=true" in ps_command
    assert "label=aios.app.id=app-1" in ps_command
    assert all(kwargs["shell"] is False for _command, kwargs in control.calls)


def test_timeout_kills_the_named_container(snapshot: Snapshot) -> None:
    executable = ExecutableSpec(
        id="slow",
        cwd=".",
        command=("python", "slow.py"),
        timeout_seconds=1,
    )
    app = app_with(AppManifest(1, "App", "", "1", executables=(executable,)))
    write_snapshot_manifest(snapshot, app.manifest)
    timeout_popen = TimeoutPopen()
    control = FakeControlRun()
    runtime = AppRuntime(
        snapshot.path.parents[3],
        control_run=control,
        popen=timeout_popen,
    )
    assert runtime.prepare(app, snapshot).ok

    result = runtime.run_executable(app, executable)

    assert result.ok is False
    assert result.timed_out is True
    assert result.exit_code == 137
    assert "container timed out" in result.stderr
    kill_command = next(
        command for command, _kwargs in control.calls if command[1] == "kill"
    )
    assert kill_command == ["docker", "kill", result.container_name]
