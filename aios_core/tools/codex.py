import subprocess

from ..workspace import resolve_workspace_path


def codex(
    task: str = None,
    timeout: float = 180,
    model: str = None,
    sandbox: str = "workspace-write",
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

    allowed_sandbox = {"read-only", "workspace-write", "danger-full-access"}
    if sandbox not in allowed_sandbox:
        return "error: sandbox must be one of read-only, workspace-write, danger-full-access"

    if not isinstance(path, str) or not path.strip():
        return "error: path must be a non-empty string"
    workdir = resolve_workspace_path(path.strip())
    if not workdir.exists():
        return f"error: path does not exist: {workdir}"
    if not workdir.is_dir():
        return f"error: path is not a directory: {workdir}"

    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--color",
        "never",
        "--sandbox",
        sandbox,
        "--cd",
        str(workdir),
        "-",
    ]
    if isinstance(model, str) and model.strip():
        cmd.extend(["--model", model.strip()])

    try:
        result = subprocess.run(
            cmd,
            input=task.strip(),
            capture_output=True,
            text=True,
            timeout=timeout_value,
            cwd=str(workdir),
        )
    except FileNotFoundError:
        return "error: codex CLI is not installed or not on PATH"
    except subprocess.TimeoutExpired as e:
        partial = ((e.stdout or "") + (e.stderr or "")).strip()
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
