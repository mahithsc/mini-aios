from __future__ import annotations

import os
import platform
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Sequence

from ..workspace import RuntimePaths, get_runtime_paths


class ExecutionSandboxUnavailable(RuntimeError):
    """Raised rather than silently running an unrestricted agent command."""


def _profile_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')


def _sandbox_profile(paths: RuntimePaths) -> str:
    writable_temporary_roots = {Path("/private/tmp"), Path("/tmp")}
    task_temporary = os.getenv("TMPDIR")
    if task_temporary:
        writable_temporary_roots.add(Path(task_temporary))

    temporary_rules = "\n".join(
        f'(allow file-write* (subpath "{_profile_path(path)}"))'
        for path in sorted(writable_temporary_roots, key=str)
    )
    return f"""
(version 1)
(allow default)
(deny file-read* (subpath "{_profile_path(paths.state)}"))
(deny file-write*)
(allow file-write* (subpath "{_profile_path(paths.applications)}"))
{temporary_rules}
(allow file-write* (subpath "/dev"))
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
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return sandbox_exec if probe.returncode == 0 else None


def sandboxed_command(
    command: Sequence[str],
    *,
    paths: RuntimePaths | None = None,
    allow_unwrapped: bool = False,
) -> list[str]:
    """Wrap a subprocess in the native macOS filesystem sandbox when present."""
    host_allows_unwrapped = os.getenv(
        "AIOS_ALLOW_UNSANDBOXED_EXECUTION", ""
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if host_allows_unwrapped:
        return list(command)
    sandbox_exec = _native_sandbox_command()
    if sandbox_exec is None:
        if allow_unwrapped:
            return list(command)
        raise ExecutionSandboxUnavailable(
            "secure subprocess sandbox is unavailable; refusing to run an "
            "unrestricted command"
        )
    return [
        sandbox_exec,
        "-p",
        _sandbox_profile(paths or get_runtime_paths()),
        *command,
    ]
