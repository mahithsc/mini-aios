import subprocess

from ..runtime_context import default_chat_files_cwd

RESET, DIM = "\033[0m", "\033[2m"


def bash(cmd: str, timeout: float = 30):
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=float(timeout),
            stdin=subprocess.DEVNULL,
            cwd=str(default_chat_files_cwd()),
        )
        out = (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") + (e.stderr or "")).strip()
        out += f"\n(timed out after {timeout:g}s)"
    if out:
        print(f"  {DIM}│ {out}{RESET}", flush=True)
    return out or "(empty)"
