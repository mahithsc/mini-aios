import subprocess

from ..workspace import ensure_workspace_dir

RESET, DIM = "\033[0m", "\033[2m"
_WORKSPACE_DIR = ensure_workspace_dir()


def bash(cmd: str, timeout: float = 30):
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=float(timeout),
            stdin=subprocess.DEVNULL,
            cwd=str(_WORKSPACE_DIR),
        )
        out = (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") + (e.stderr or "")).strip()
        out += f"\n(timed out after {timeout:g}s)"
    if out:
        print(f"  {DIM}│ {out}{RESET}", flush=True)
    return out or "(empty)"
