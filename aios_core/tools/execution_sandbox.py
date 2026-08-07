from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from ..apps.paths import protected_app_roots
from ..workspace import RuntimePaths, get_runtime_paths


class ExecutionSandboxUnavailable(RuntimeError):
    """Raised rather than silently running an unrestricted agent command."""


def _profile_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


def _sandbox_profile(
    paths: RuntimePaths,
    app_roots: Sequence[Path] | None = None,
) -> str:
    writable_temporary_roots = {Path("/private/tmp"), Path("/tmp")}
    task_temporary = os.getenv("TMPDIR")
    if task_temporary:
        writable_temporary_roots.add(Path(task_temporary))

    temporary_rules = "\n".join(
        f'(allow file-write* (subpath "{_profile_path(path)}"))'
        for path in sorted(writable_temporary_roots, key=str)
    )
    protected_rules = "\n".join(
        (
            f'(deny file-read* (subpath "{_profile_path(path)}"))\n'
            f'(deny file-write* (subpath "{_profile_path(path)}"))\n'
            f'(deny file-map-executable (subpath "{_profile_path(path)}"))\n'
            f'(deny process-exec (subpath "{_profile_path(path)}"))'
        )
        for path in (app_roots if app_roots is not None else protected_app_roots(paths))
    )
    applications_pattern = re.escape(_profile_path(paths.applications))
    manifest_rule = (
        f'(deny file-write* (regex #"^{applications_pattern}/[^/]+/app\\.json$"))'
    )
    return f"""
(version 1)
(allow default)
(deny file-read* (subpath "{_profile_path(paths.state)}"))
(deny file-write*)
(allow file-write* (subpath "{_profile_path(paths.applications)}"))
{temporary_rules}
(allow file-write* (subpath "/dev"))
{manifest_rule}
{protected_rules}
""".strip()


@lru_cache(maxsize=1)
def _native_sandbox_command() -> str | None:
    if platform.system() != "Darwin":
        return None
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        return None
    try:
        probe = subprocess.run(
            [
                sandbox_exec,
                "-p",
                "(version 1) (allow default)",
                "/usr/bin/true",
            ],
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return sandbox_exec if probe.returncode == 0 else None


def sandboxed_command(
    command: Sequence[str],
    *,
    paths: RuntimePaths | None = None,
    allow_unwrapped: bool = False,
    protected_roots: Sequence[Path] | None = None,
) -> list[str]:
    """Wrap a subprocess in the native macOS filesystem sandbox when present."""
    runtime_paths = paths or get_runtime_paths()
    app_roots = (
        tuple(protected_roots)
        if protected_roots is not None
        else protected_app_roots(runtime_paths)
    )
    sandbox_exec = _native_sandbox_command()
    if sandbox_exec is None:
        if allow_unwrapped:
            if app_roots:
                raise ExecutionSandboxUnavailable(
                    "unwrapped host execution is disabled while managed Apps exist"
                )
            return list(command)
        raise ExecutionSandboxUnavailable(
            "secure subprocess sandbox is unavailable; refusing to run an "
            "unrestricted command"
        )
    return [
        sandbox_exec,
        "-p",
        _sandbox_profile(runtime_paths, app_roots),
        *command,
    ]
