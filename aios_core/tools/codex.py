import subprocess

from ..runtime_context import resolve_codex_workdir


def resolve_chat_files_path(path: str):
    return resolve_codex_workdir(path)


def codex(
    task: str = None,
    timeout: float = 180,
    model: str = None,
    path: str = ".",
):
    """
    Delegate a task to Codex CLI in non-interactive mode.
    """
    if not isinstance(task, str) or not task.strip():
        return "error: task is required"

    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        return "error: timeout must be a number"
    if timeout_value <= 0:
        return "error: timeout must be > 0"

    if not isinstance(path, str) or not path.strip():
        return "error: path must be a non-empty string"
    try:
        workdir = resolve_chat_files_path(path.strip()).resolve()
    except ValueError as exc:
        return f"error: {exc}"
    if not workdir.exists():
        return f"error: path does not exist: {workdir}"
    if not workdir.is_dir():
        return f"error: path is not a directory: {workdir}"

    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "danger-full-access",
    ]
    if isinstance(model, str) and model.strip():
        cmd.extend(["--model", model.strip()])
    cmd.append(task.strip())

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_value,
            cwd=str(workdir),
        )
    except FileNotFoundError:
        return "error: codex CLI is not installed or not on PATH"
    except subprocess.TimeoutExpired as e:
        def _to_str(v):
            if v is None:
                return ""
            if isinstance(v, (bytes, bytearray)):
                return v.decode(errors="replace")
            return str(v)

        partial = (_to_str(e.stdout) + _to_str(e.stderr)).strip()
        if partial:
            return f"{partial}\n(error: codex timed out after {timeout_value:g}s)"
        return f"error: codex timed out after {timeout_value:g}s"
    except Exception as e:
        return f"error: codex failed -- {e}"

    out = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        if out:
            return f"error: codex exit {result.returncode} -- {out}"
        return f"error: codex exit {result.returncode}"
    return out or "(empty)"
