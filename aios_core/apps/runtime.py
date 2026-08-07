from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .manifest import ManifestValidationError, load_manifest

DEFAULT_IMAGE = "aios-app-runtime:v1"
DEFAULT_OUTPUT_BYTES = 64 * 1024
DEFAULT_TMPFS_BYTES = 64 * 1024 * 1024
DEFAULT_IMAGE_BUILD_TIMEOUT = 30 * 60
DEFAULT_RUNTIME_STORAGE_BYTES = 1024 * 1024 * 1024
DEFAULT_DATA_STORAGE_BYTES = 512 * 1024 * 1024
DEFAULT_RUNTIME_STORAGE_FILES = 100_000
DEFAULT_DATA_STORAGE_FILES = 50_000
MAX_SNAPSHOT_FILES = 10_000
MAX_SNAPSHOT_BYTES = 100 * 1024 * 1024

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESERVED_ENV = {
    "HOME",
    "HOSTNAME",
    "NODE_PATH",
    "PATH",
    "PWD",
    "PYTHONHOME",
    "PYTHONPATH",
    "TMPDIR",
}


class AppRuntimeError(RuntimeError):
    """Base class for App runtime failures."""


class DockerUnavailableError(AppRuntimeError):
    """Raised when Docker cannot be used. App code never falls back to the host."""


class RuntimeConfigurationError(AppRuntimeError, ValueError):
    """Raised when runtime inputs would produce an invalid or unsafe container."""


class ContainerLaunchError(AppRuntimeError):
    """Raised when Docker exists but the client process cannot be started."""


@dataclass(frozen=True)
class RuntimeResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool = False
    container_name: str | None = None
    runtime_path: str | None = None


@dataclass(frozen=True)
class StopResult:
    ok: bool
    stopped: int
    stderr: str = ""


@dataclass(frozen=True)
class _Settings:
    network: bool = False
    persistent_data: bool = False
    memory_mb: int = 512
    cpus: float = 1.0
    max_processes: int = 64


class _BoundedBytes:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()
        self.truncated = False
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        with self._lock:
            remaining = self._limit - len(self._data)
            if remaining > 0:
                self._data.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.truncated = True

    def text(self) -> str:
        value = bytes(self._data).decode("utf-8", errors="replace")
        if self.truncated:
            return value + "\n[output truncated]"
        return value


def _value(obj: object, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _safe_component(value: object, field: str) -> str:
    if value is None:
        raise RuntimeConfigurationError(f"{field} is required")
    text = str(value)
    if not _SAFE_COMPONENT.fullmatch(text):
        raise RuntimeConfigurationError(f"{field} is not a safe identifier")
    return text


def _relative_cwd(value: object) -> str:
    text = str(value or ".")
    if "\\" in text:
        raise RuntimeConfigurationError("container cwd must use forward slashes")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        raise RuntimeConfigurationError("container cwd must stay inside /app")
    return "/app" if text == "." else f"/app/{path.as_posix()}"


def _command(value: object, *, field: str = "command") -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise RuntimeConfigurationError(f"{field} must be an argument array")
    result = [str(part) for part in value]
    if not result or any(not part or "\x00" in part for part in result):
        raise RuntimeConfigurationError(f"{field} contains an invalid argument")
    if len(result) > 128 or any(len(part) > 8192 for part in result):
        raise RuntimeConfigurationError(f"{field} exceeds runtime limits")
    return result


def _manifest(app: object) -> object:
    value = _value(app, "manifest", "manifest_json")
    if value is None:
        return app
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeConfigurationError("manifest_json is invalid") from exc
    return value


def _settings(app: object) -> _Settings:
    runtime = _value(_manifest(app), "runtime", default={})
    try:
        memory_mb = int(_value(runtime, "memory_mb", "memoryMb", default=512))
        cpus = float(_value(runtime, "cpus", default=1.0))
        max_processes = int(
            _value(runtime, "max_processes", "maxProcesses", default=64)
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeConfigurationError("runtime limits must be numeric") from exc
    if not 64 <= memory_mb <= 16384:
        raise RuntimeConfigurationError(
            "runtime memory must be between 64 and 16384 MB"
        )
    if not 0.1 <= cpus <= 16:
        raise RuntimeConfigurationError("runtime cpus must be between 0.1 and 16")
    if not 1 <= max_processes <= 1024:
        raise RuntimeConfigurationError(
            "runtime process limit must be between 1 and 1024"
        )
    return _Settings(
        network=bool(_value(runtime, "network", default=False)),
        persistent_data=bool(
            _value(runtime, "persistent_data", "persistentData", default=False)
        ),
        memory_mb=memory_mb,
        cpus=cpus,
        max_processes=max_processes,
    )


class AppRuntime:
    """Run validated App snapshots through a constrained Docker CLI boundary."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        image: str = DEFAULT_IMAGE,
        docker_binary: str = "docker",
        build_context: str | Path | None = None,
        output_bytes: int = DEFAULT_OUTPUT_BYTES,
        tmpfs_bytes: int = DEFAULT_TMPFS_BYTES,
        runtime_storage_bytes: int = DEFAULT_RUNTIME_STORAGE_BYTES,
        data_storage_bytes: int = DEFAULT_DATA_STORAGE_BYTES,
        runtime_storage_files: int = DEFAULT_RUNTIME_STORAGE_FILES,
        data_storage_files: int = DEFAULT_DATA_STORAGE_FILES,
        control_run: Callable[..., Any] = subprocess.run,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.state_root = Path(state_root).expanduser().resolve()
        if self.state_root == Path(self.state_root.anchor):
            raise RuntimeConfigurationError(
                "Apps state root cannot be a filesystem root"
            )
        self.snapshots_root = self.state_root / "snapshots"
        self.runtime_root = self.state_root / "runtime"
        self.data_root = self.state_root / "data"
        self.image = image
        self.docker_binary = docker_binary
        self.build_context = (
            Path(
                build_context
                or Path(__file__).resolve().parents[2] / "containers" / "app-runtime"
            )
            .expanduser()
            .resolve()
        )
        self.output_bytes = max(1024, min(int(output_bytes), 1024 * 1024))
        self.tmpfs_bytes = max(1024 * 1024, min(int(tmpfs_bytes), 512 * 1024 * 1024))
        self.runtime_storage_bytes = max(
            1024 * 1024,
            min(int(runtime_storage_bytes), 8 * 1024 * 1024 * 1024),
        )
        self.data_storage_bytes = max(
            1024 * 1024,
            min(int(data_storage_bytes), 8 * 1024 * 1024 * 1024),
        )
        self.runtime_storage_files = max(
            1_000,
            min(int(runtime_storage_files), 1_000_000),
        )
        self.data_storage_files = max(
            1_000,
            min(int(data_storage_files), 1_000_000),
        )
        self._control_run = control_run
        self._popen = popen

    def available(self) -> bool:
        try:
            completed = self._control_run(
                [self.docker_binary, "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                shell=False,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def image_available(self) -> bool:
        if not self.available():
            return False
        try:
            completed = self._control_run(
                [self.docker_binary, "image", "inspect", self.image],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                shell=False,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return False
        return completed.returncode == 0

    def ensure_image(self) -> None:
        """Build the trusted generic runtime image on first executable use."""

        self._require_daemon()
        if self.image_available():
            return
        dockerfile = self.build_context / "Dockerfile"
        if not dockerfile.is_file():
            raise RuntimeConfigurationError(
                f"App runtime image is missing and build context is unavailable: {dockerfile}"
            )
        try:
            completed = self._control_run(
                [
                    self.docker_binary,
                    "build",
                    "--tag",
                    self.image,
                    str(self.build_context),
                ],
                capture_output=True,
                text=True,
                timeout=DEFAULT_IMAGE_BUILD_TIMEOUT,
                check=False,
                shell=False,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
            raise DockerUnavailableError(
                "Docker became unavailable while building the App runtime image"
            ) from exc
        if completed.returncode != 0:
            detail = (
                completed.stderr or completed.stdout or "unknown build error"
            ).strip()
            raise ContainerLaunchError(
                f"could not build {self.image}: {detail[-4000:]}"
            )

    def prepare(
        self,
        app: object,
        snapshot: object,
        *,
        network_approved: bool = False,
    ) -> RuntimeResult:
        app_id = self._app_id(app)
        snapshot_path, content_hash = self._snapshot(snapshot)
        self._require_snapshot_identity(app_id, snapshot_path, content_hash)
        self._verify_snapshot_content(snapshot_path, content_hash)
        manifest = self._snapshot_manifest(snapshot_path)
        settings = _settings(manifest)
        steps = _value(manifest, "prepare", default=()) or ()
        if isinstance(steps, (str, bytes)) or not isinstance(steps, Sequence):
            raise RuntimeConfigurationError("prepare must be an array")
        if len(steps) > 32:
            raise RuntimeConfigurationError("prepare cannot contain more than 32 steps")
        mcp_servers = _value(manifest, "mcp_servers", "mcpServers", default=()) or ()
        executables = _value(manifest, "executables", default=()) or ()
        if steps or mcp_servers or executables:
            self.ensure_image()

        target = self._runtime_path(app_id, content_hash)
        if self._prepared(target, content_hash):
            self._require_storage_within_limit(
                target,
                self.runtime_storage_bytes,
                self.runtime_storage_files,
                "prepared runtime",
            )
            return RuntimeResult(True, 0, "", "", False, runtime_path=str(target))
        if target.exists():
            raise RuntimeConfigurationError(
                "runtime directory exists without a valid preparation marker"
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{content_hash}.prepare-{uuid.uuid4().hex}"
        self._create_writable_directory(temporary)
        results: list[RuntimeResult] = []
        try:
            for index, step in enumerate(steps):
                step_network = bool(_value(step, "network", default=False))
                if step_network and not network_approved:
                    raise RuntimeConfigurationError(
                        "prepare requested network access without approval"
                    )
                command = _command(_value(step, "command"), field="prepare command")
                name = self._container_name(app_id, f"prepare-{index}")
                docker_command = self._docker_command(
                    app_id=app_id,
                    content_hash=content_hash,
                    component=f"prepare-{index}",
                    container_name=name,
                    snapshot_path=snapshot_path,
                    runtime_path=temporary,
                    data_path=None,
                    settings=settings,
                    network=step_network,
                    writable_runtime=True,
                    interactive=False,
                    cwd="/app",
                    environment={},
                    command=command,
                )
                result = self._execute(
                    docker_command,
                    name=name,
                    timeout=3600,
                    storage_limits=(
                        (
                            temporary,
                            self.runtime_storage_bytes,
                            self.runtime_storage_files,
                            "prepared runtime",
                        ),
                    ),
                )
                results.append(result)
                if not result.ok:
                    return self._combine(results, runtime_path=None)

            marker = temporary / ".aios-prepared.json"
            marker.write_text(
                json.dumps({"contentHash": content_hash}, sort_keys=True),
                encoding="utf-8",
            )
            try:
                temporary.replace(target)
            except FileExistsError:
                if not self._prepared(target, content_hash):
                    raise
            return self._combine(results, runtime_path=str(target))
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def run_executable(
        self,
        app: object,
        executable: object,
        args: Sequence[object] = (),
        *,
        network_approved: bool = False,
    ) -> RuntimeResult:
        self._require_runtime_image()
        app_id = self._app_id(app)
        snapshot_path, content_hash = self._active_snapshot(app)
        runtime_path = self._require_prepared(app_id, content_hash)
        settings = _settings(self._snapshot_manifest(snapshot_path))
        if settings.network and not network_approved:
            raise RuntimeConfigurationError(
                "App requested network access without approval"
            )
        component = _safe_component(_value(executable, "id"), "executable id")
        dynamic_args = _command(list(args), field="executable args") if args else []
        command = _command(_value(executable, "command")) + dynamic_args
        try:
            timeout = int(
                _value(executable, "timeout_seconds", "timeoutSeconds", default=60)
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigurationError(
                "executable timeout must be an integer"
            ) from exc
        if not 1 <= timeout <= 3600:
            raise RuntimeConfigurationError(
                "executable timeout must be between 1 and 3600"
            )
        data_path = self._data_path(app_id) if settings.persistent_data else None
        if data_path is not None:
            self._create_writable_directory(data_path)
            self._require_storage_within_limit(
                data_path,
                self.data_storage_bytes,
                self.data_storage_files,
                "persistent App data",
            )
        name = self._container_name(app_id, f"exec-{component}", unique=True)
        docker_command = self._docker_command(
            app_id=app_id,
            content_hash=content_hash,
            component=component,
            container_name=name,
            snapshot_path=snapshot_path,
            runtime_path=runtime_path,
            data_path=data_path,
            settings=settings,
            network=settings.network,
            writable_runtime=False,
            interactive=False,
            cwd=_relative_cwd(_value(executable, "cwd", default=".")),
            environment=_value(executable, "env", default={}) or {},
            command=command,
        )
        storage_limits = (
            (
                (
                    data_path,
                    self.data_storage_bytes,
                    self.data_storage_files,
                    "persistent App data",
                ),
            )
            if data_path is not None
            else ()
        )
        result = self._execute(
            docker_command,
            name=name,
            timeout=timeout,
            storage_limits=storage_limits,
        )
        return RuntimeResult(**{**result.__dict__, "runtime_path": str(runtime_path)})

    def mcp_server_parameters(
        self,
        app: object,
        server: object,
        *,
        network_approved: bool = False,
        verified_snapshot: object | None = None,
    ) -> dict[str, object]:
        self._require_runtime_image()
        app_id = self._app_id(app)
        if verified_snapshot is None:
            snapshot_path, content_hash = self._active_snapshot(app)
        else:
            snapshot_path, content_hash = self._snapshot(verified_snapshot)
            active_hash = _value(
                _value(app, "app", "record", default=app),
                "active_hash",
                "activeHash",
            )
            if active_hash != content_hash:
                raise RuntimeConfigurationError(
                    "verified MCP snapshot is not the active App snapshot"
                )
            self._require_snapshot_identity(app_id, snapshot_path, content_hash)
        runtime_path = self._require_prepared(app_id, content_hash)
        settings = _settings(self._snapshot_manifest(snapshot_path))
        if settings.network and not network_approved:
            raise RuntimeConfigurationError(
                "App requested network access without approval"
            )
        component = _safe_component(_value(server, "id"), "MCP server id")
        data_path = self._data_path(app_id) if settings.persistent_data else None
        if data_path is not None:
            self._create_writable_directory(data_path)
            self._require_storage_within_limit(
                data_path,
                self.data_storage_bytes,
                self.data_storage_files,
                "persistent App data",
            )
        name = self._container_name(app_id, f"mcp-{component}", unique=True)
        args = self._docker_command(
            app_id=app_id,
            content_hash=content_hash,
            component=component,
            container_name=name,
            snapshot_path=snapshot_path,
            runtime_path=runtime_path,
            data_path=data_path,
            data_readonly=True,
            settings=settings,
            network=settings.network,
            writable_runtime=False,
            interactive=True,
            cwd=_relative_cwd(_value(server, "cwd", default=".")),
            environment=_value(server, "env", default={}) or {},
            command=_command(_value(server, "command")),
        )[1:]
        return {
            "command": self.docker_binary,
            "args": args,
            "env": {"PATH": os.environ.get("PATH", os.defpath)},
            "cwd": None,
        }

    def stop_app(self, app_id: object) -> StopResult:
        self._require_daemon()
        safe_id = _safe_component(app_id, "app id")
        try:
            listed = self._control_run(
                [
                    self.docker_binary,
                    "ps",
                    "-q",
                    "--filter",
                    "label=aios.managed=true",
                    "--filter",
                    f"label=aios.app.id={safe_id}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                shell=False,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
            raise DockerUnavailableError("Docker became unavailable") from exc
        if listed.returncode != 0:
            return StopResult(False, 0, listed.stderr.strip())
        container_ids = [line for line in listed.stdout.splitlines() if line.strip()]
        if not container_ids:
            return StopResult(True, 0)
        stopped = self._control_run(
            [self.docker_binary, "stop", "--time", "5", *container_ids],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            shell=False,
        )
        count = len([line for line in stopped.stdout.splitlines() if line.strip()])
        return StopResult(stopped.returncode == 0, count, stopped.stderr.strip())

    def clear_data(self, app_id: object) -> bool:
        safe_id = _safe_component(app_id, "app id")
        candidate = self.data_root / safe_id
        if candidate.is_symlink():
            candidate.unlink()
            return True
        path = self._data_path(safe_id)
        if not path.exists():
            return False
        if not path.is_dir():
            raise RuntimeConfigurationError("App data path is not a directory")
        shutil.rmtree(path)
        return True

    def _require_daemon(self) -> None:
        if not self.available():
            raise DockerUnavailableError(
                "Docker is unavailable; executable Apps cannot run on the host"
            )

    def _require_runtime_image(self) -> None:
        self._require_daemon()
        if not self.image_available():
            raise DockerUnavailableError(
                f"App runtime image is unavailable: {self.image}; prepare the App again"
            )

    def _app_id(self, app: object) -> str:
        record = _value(app, "app", "record", default=app)
        return _safe_component(_value(record, "id", "slug"), "app id")

    def _snapshot(self, snapshot: object) -> tuple[Path, str]:
        raw_path = (
            snapshot if isinstance(snapshot, (str, Path)) else _value(snapshot, "path")
        )
        if raw_path is None:
            raise RuntimeConfigurationError("snapshot path is required")
        path = Path(raw_path).expanduser().resolve()
        try:
            path.relative_to(self.snapshots_root.resolve())
        except ValueError as exc:
            raise RuntimeConfigurationError(
                "snapshot must be inside Apps snapshot state"
            ) from exc
        if not path.is_dir():
            raise RuntimeConfigurationError(
                "snapshot path must be an existing directory"
            )
        content_hash = _value(snapshot, "content_hash", "contentHash", "hash")
        if content_hash is None:
            content_hash = path.parent.name if path.name == "app" else path.name
        return path, _safe_component(content_hash, "snapshot hash")

    def _active_snapshot(self, app: object) -> tuple[Path, str]:
        record = _value(app, "app", "record", default=app)
        content_hash = _value(record, "active_hash", "activeHash")
        if not content_hash:
            raise RuntimeConfigurationError("App has no active snapshot")
        app_id = self._app_id(app)
        explicit = _value(app, "snapshot_path", "active_snapshot_path")
        path = (
            Path(explicit)
            if explicit
            else self.snapshots_root / app_id / str(content_hash) / "app"
        )
        snapshot_path, snapshot_hash = self._snapshot(
            {"path": path, "content_hash": content_hash}
        )
        self._require_snapshot_identity(app_id, snapshot_path, snapshot_hash)
        self._verify_snapshot_content(snapshot_path, snapshot_hash)
        return snapshot_path, snapshot_hash

    def _require_snapshot_identity(
        self,
        app_id: str,
        snapshot_path: Path,
        content_hash: str,
    ) -> None:
        expected = (self.snapshots_root / app_id / content_hash / "app").resolve()
        if snapshot_path != expected:
            raise RuntimeConfigurationError(
                "snapshot path does not match its App id and content hash"
            )

    def _verify_snapshot_content(
        self,
        snapshot_path: Path,
        content_hash: str,
    ) -> None:
        # Older unit-test fixtures use symbolic hashes. Registry-created Apps
        # always use SHA-256 and are verified before any container is started.
        if not _SHA256.fullmatch(content_hash):
            return

        files: list[tuple[str, Path, int, bool]] = []
        total_size = 0

        def visit(directory: Path, relative_directory: Path) -> None:
            nonlocal total_size
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as exc:
                raise RuntimeConfigurationError(
                    "App snapshot could not be inspected"
                ) from exc
            for entry in entries:
                relative = relative_directory / entry.name
                relative_text = relative.as_posix()
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    raise RuntimeConfigurationError(
                        f"App snapshot path could not be inspected: {relative_text}"
                    ) from exc
                mode = metadata.st_mode
                if stat.S_ISLNK(mode):
                    raise RuntimeConfigurationError(
                        f"App snapshot contains a symlink: {relative_text}"
                    )
                if stat.S_ISDIR(mode):
                    visit(Path(entry.path), relative)
                    continue
                if not stat.S_ISREG(mode) or metadata.st_nlink != 1:
                    raise RuntimeConfigurationError(
                        f"App snapshot contains an unsafe file: {relative_text}"
                    )
                total_size += metadata.st_size
                if len(files) >= MAX_SNAPSHOT_FILES or total_size > MAX_SNAPSHOT_BYTES:
                    raise RuntimeConfigurationError(
                        "App snapshot exceeds runtime limits"
                    )
                files.append(
                    (
                        relative_text,
                        Path(entry.path),
                        metadata.st_size,
                        bool(mode & 0o111),
                    )
                )

        visit(snapshot_path, Path())
        digest = hashlib.sha256()
        digest.update(b"aios-app-snapshot-v1\0")
        for relative_path, path, expected_size, executable in files:
            path_bytes = relative_path.encode("utf-8")
            digest.update(len(path_bytes).to_bytes(4, "big"))
            digest.update(path_bytes)
            digest.update(expected_size.to_bytes(8, "big"))
            digest.update(b"x" if executable else b"-")
            copied = 0
            try:
                with path.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        copied += len(chunk)
                metadata = path.lstat()
            except OSError as exc:
                raise RuntimeConfigurationError(
                    f"App snapshot path could not be read: {relative_path}"
                ) from exc
            if (
                copied != expected_size
                or metadata.st_size != expected_size
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or bool(metadata.st_mode & 0o111) != executable
            ):
                raise RuntimeConfigurationError(
                    f"App snapshot changed during verification: {relative_path}"
                )
        actual_hash = digest.hexdigest()
        if actual_hash != content_hash:
            raise RuntimeConfigurationError(
                "App snapshot content does not match its registered hash"
            )

    def _runtime_path(self, app_id: str, content_hash: str) -> Path:
        return self._contained_path(
            self.runtime_root,
            self.runtime_root / app_id / content_hash,
        )

    def _data_path(self, app_id: str) -> Path:
        return self._contained_path(self.data_root, self.data_root / app_id)

    def _snapshot_manifest(self, snapshot_path: Path) -> object:
        try:
            return load_manifest(snapshot_path / "app.json")
        except ManifestValidationError as exc:
            raise RuntimeConfigurationError(
                "active App snapshot contains an invalid manifest"
            ) from exc

    def _contained_path(self, root: Path, candidate: Path) -> Path:
        resolved_root = root.resolve()
        resolved = candidate.resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            raise RuntimeConfigurationError(
                "App runtime path escaped its state root"
            ) from exc
        return resolved

    def _prepared(self, path: Path, content_hash: str) -> bool:
        marker = path / ".aios-prepared.json"
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return value == {"contentHash": content_hash}

    def _require_prepared(self, app_id: str, content_hash: str) -> Path:
        path = self._runtime_path(app_id, content_hash)
        if not self._prepared(path, content_hash):
            raise RuntimeConfigurationError("active App snapshot has not been prepared")
        self._require_storage_within_limit(
            path,
            self.runtime_storage_bytes,
            self.runtime_storage_files,
            "prepared runtime",
        )
        return path

    def _require_storage_within_limit(
        self,
        path: Path,
        byte_limit: int,
        file_limit: int,
        label: str,
    ) -> None:
        violation = self._storage_violation(((path, byte_limit, file_limit, label),))
        if violation:
            raise RuntimeConfigurationError(violation)

    def _storage_violation(
        self,
        limits: Sequence[tuple[Path, int, int, str]],
    ) -> str | None:
        for root, byte_limit, file_limit, label in limits:
            total = 0
            entries_seen = 0
            pending = [root]
            try:
                while pending:
                    directory = pending.pop()
                    for entry in os.scandir(directory):
                        entries_seen += 1
                        if entries_seen > file_limit:
                            return f"{label} exceeded its {file_limit}-entry limit"
                        metadata = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(metadata.st_mode):
                            pending.append(Path(entry.path))
                        else:
                            total += metadata.st_size
                            if total > byte_limit:
                                return f"{label} exceeded its {byte_limit}-byte limit"
            except FileNotFoundError:
                continue
            except OSError:
                return f"{label} could not be measured safely"
        return None

    def _create_writable_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir() or path.is_symlink():
            raise RuntimeConfigurationError("App runtime path must be a real directory")
        path.chmod(0o700)
        if os.geteuid() == 0:
            os.chown(path, 65532, 65532)

    def _container_user(self) -> str:
        if os.geteuid() == 0:
            return "65532:65532"
        return f"{os.geteuid()}:{os.getegid()}"

    def _mount(self, source: Path, destination: str, *, readonly: bool) -> str:
        resolved = source.resolve()
        if "," in str(resolved) or "\n" in str(resolved):
            raise RuntimeConfigurationError(
                "Apps state paths cannot contain commas or newlines"
            )
        value = f"type=bind,src={resolved},dst={destination}"
        return value + (",readonly" if readonly else "")

    def _environment_args(self, environment: object) -> list[str]:
        if not isinstance(environment, Mapping):
            raise RuntimeConfigurationError("environment must be an object")
        if len(environment) > 64:
            raise RuntimeConfigurationError("environment exceeds 64 entries")
        values = {
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "PATH": "/runtime/venv/bin:/runtime/node/bin:/usr/local/bin:/usr/bin:/bin",
            "PYTHONPATH": "/app:/runtime/python",
            "NODE_PATH": "/runtime/node/lib/node_modules:/runtime/node_modules",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_TARGET": "/runtime/python",
            "UV_CACHE_DIR": "/tmp/uv-cache",
        }
        for raw_name, raw_value in environment.items():
            name = str(raw_name)
            value = str(raw_value)
            if (
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                or name in _RESERVED_ENV
                or name.startswith("AIOS_")
                or "\x00" in value
            ):
                raise RuntimeConfigurationError(
                    f"environment variable {name!r} is reserved or invalid"
                )
            values[name] = value
        result: list[str] = []
        for name, value in values.items():
            result.extend(["--env", f"{name}={value}"])
        return result

    def _docker_command(
        self,
        *,
        app_id: str,
        content_hash: str,
        component: str,
        container_name: str,
        snapshot_path: Path,
        runtime_path: Path,
        data_path: Path | None,
        data_readonly: bool = False,
        settings: _Settings,
        network: bool,
        writable_runtime: bool,
        interactive: bool,
        cwd: str,
        environment: object,
        command: Sequence[str],
    ) -> list[str]:
        result = [self.docker_binary, "run", "--rm", "--pull", "never"]
        if interactive:
            result.append("-i")
        result.extend(
            [
                "--name",
                container_name,
                "--label",
                "aios.managed=true",
                "--label",
                f"aios.app.id={app_id}",
                "--label",
                f"aios.app.hash={content_hash}",
                "--label",
                f"aios.app.component={component}",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                self._container_user(),
                "--memory",
                f"{settings.memory_mb}m",
                "--memory-swap",
                f"{settings.memory_mb}m",
                "--cpus",
                f"{settings.cpus:g}",
                "--pids-limit",
                str(settings.max_processes),
                "--network",
                "bridge" if network else "none",
                "--tmpfs",
                f"/tmp:rw,nosuid,nodev,noexec,size={self.tmpfs_bytes}",
                "--mount",
                self._mount(snapshot_path, "/app", readonly=True),
                "--mount",
                self._mount(runtime_path, "/runtime", readonly=not writable_runtime),
            ]
        )
        if data_path is not None:
            result.extend(
                ["--mount", self._mount(data_path, "/data", readonly=data_readonly)]
            )
        result.extend(self._environment_args(environment))
        result.extend(["--workdir", cwd, "--stop-timeout", "5", self.image])
        result.extend(command)
        return result

    def _container_name(
        self, app_id: str, component: str, *, unique: bool = False
    ) -> str:
        raw = f"{app_id}-{component}"
        digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
        prefix = re.sub(r"[^a-z0-9_.-]+", "-", raw.lower()).strip("-.")[:40]
        suffix = f"-{uuid.uuid4().hex[:8]}" if unique else ""
        return f"aios-app-{prefix}-{digest}{suffix}"

    def _execute(
        self,
        command: list[str],
        *,
        name: str,
        timeout: int,
        storage_limits: Sequence[tuple[Path, int, int, str]] = (),
    ) -> RuntimeResult:
        initial_violation = self._storage_violation(storage_limits)
        if initial_violation:
            raise RuntimeConfigurationError(initial_violation)
        try:
            process = self._popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise DockerUnavailableError("Docker became unavailable") from exc
        except OSError as exc:
            raise ContainerLaunchError(f"could not launch Docker: {exc}") from exc

        stdout = _BoundedBytes(self.output_bytes)
        stderr = _BoundedBytes(self.output_bytes)

        def drain(stream: Any, destination: _BoundedBytes) -> None:
            if stream is None:
                return
            try:
                while chunk := stream.read(8192):
                    destination.append(chunk)
            except (OSError, ValueError):
                return

        threads = [
            threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()

        monitor_stop = threading.Event()
        storage_violations: list[str] = []

        def monitor_storage() -> None:
            while not monitor_stop.wait(0.25):
                violation = self._storage_violation(storage_limits)
                if violation:
                    storage_violations.append(violation)
                    self._kill_container(name)
                    return

        storage_thread = threading.Thread(target=monitor_storage, daemon=True)
        if storage_limits:
            storage_thread.start()

        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_container(name)
            try:
                exit_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait(timeout=5)
        finally:
            monitor_stop.set()
        if storage_limits:
            storage_thread.join(timeout=2)
            final_violation = self._storage_violation(storage_limits)
            if final_violation and not storage_violations:
                storage_violations.append(final_violation)
        for thread in threads:
            thread.join(timeout=2)
        stderr_text = stderr.text()
        if timed_out:
            stderr_text = (
                stderr_text + "\n" if stderr_text else ""
            ) + "container timed out"
        if storage_violations:
            stderr_text = (
                stderr_text + "\n" if stderr_text else ""
            ) + storage_violations[0]
        return RuntimeResult(
            ok=exit_code == 0 and not timed_out and not storage_violations,
            exit_code=exit_code,
            stdout=stdout.text(),
            stderr=stderr_text,
            timed_out=timed_out,
            truncated=stdout.truncated or stderr.truncated,
            container_name=name,
        )

    def _kill_container(self, name: str) -> None:
        try:
            self._control_run(
                [self.docker_binary, "kill", name],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                shell=False,
            )
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return

    def _combine(
        self,
        results: Sequence[RuntimeResult],
        *,
        runtime_path: str | None,
    ) -> RuntimeResult:
        if not results:
            return RuntimeResult(True, 0, "", "", False, runtime_path=runtime_path)
        stdout, stdout_cut = self._bounded_text("\n".join(r.stdout for r in results))
        stderr, stderr_cut = self._bounded_text("\n".join(r.stderr for r in results))
        last = results[-1]
        return RuntimeResult(
            ok=all(result.ok for result in results),
            exit_code=last.exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=any(result.timed_out for result in results),
            truncated=stdout_cut
            or stderr_cut
            or any(result.truncated for result in results),
            container_name=last.container_name,
            runtime_path=runtime_path,
        )

    def _bounded_text(self, value: str) -> tuple[str, bool]:
        encoded = value.encode("utf-8")
        if len(encoded) <= self.output_bytes:
            return value, False
        truncated = encoded[: self.output_bytes].decode("utf-8", errors="replace")
        return truncated + "\n[output truncated]", True
