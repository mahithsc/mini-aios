import subprocess

from ..runtime_context import resolve_chat_files_path


def codex(
    task: str = None,
    timeout: float = 180,
    model: str = None,
    path: str = ".",
    fc=None,
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
    workdir = resolve_chat_files_path(path.strip())
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
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(workdir),
        )
        add_cancel_callback = getattr(fc, "add_cancel_callback", None)
        if callable(add_cancel_callback):
            add_cancel_callback(
                lambda: process.kill() if process.poll() is None else None
            )
        stdout, stderr = process.communicate(timeout=timeout_value)
    except FileNotFoundError:
        return "error: codex CLI is not installed or not on PATH"
    except subprocess.TimeoutExpired as e:
        process.kill()
        stdout, stderr = process.communicate()

        def _to_str(v):
            if v is None:
                return ""
            if isinstance(v, (bytes, bytearray)):
                return v.decode(errors="replace")
            return str(v)

        partial = (_to_str(stdout) + _to_str(stderr)).strip()
        if not partial:
            partial = (_to_str(e.stdout) + _to_str(e.stderr)).strip()
        if partial:
            return f"{partial}\n(error: codex timed out after {timeout_value:g}s)"
        return f"error: codex timed out after {timeout_value:g}s"
    except Exception as e:
        return f"error: codex failed -- {e}"

    out = (stdout + stderr).strip()
    if process.returncode != 0:
        if out:
            return f"error: codex exit {process.returncode} -- {out}"
        return f"error: codex exit {process.returncode}"
    return out or "(empty)"
