import os
import re
import subprocess
import glob as globlib

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"


def read(path: str, offset: int = 0, limit: int = None):
    try:
        lines = open(path).readlines()
    except FileNotFoundError:
        return f"error: file not found: {path}"
    except IsADirectoryError:
        return f"error: path is a directory: {path}"
    if limit is None:
        limit = len(lines)
    selected = lines[offset : offset + limit]
    return "".join(f"{offset + idx + 1:4}| {line}" for idx, line in enumerate(selected))


def write(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    return "ok"


def edit(path: str, old: str, new: str, all: bool = False):
    text = open(path).read()
    if old not in text:
        return "error: old_string not found"
    count = text.count(old)
    if not all and count > 1:
        return f"error: old_string appears {count} times, must be unique (use all=true)"
    replacement = text.replace(old, new) if all else text.replace(old, new, 1)
    with open(path, "w") as f:
        f.write(replacement)
    return "ok"


def glob(pat: str, path: str = "."):
    pattern = (path + "/" + pat).replace("//", "/")
    files = globlib.glob(pattern, recursive=True)
    files = sorted(
        files,
        key=lambda f: os.path.getmtime(f) if os.path.isfile(f) else 0,
        reverse=True,
    )
    return "\n".join(files) or "none"


def grep(pat: str, path: str = "."):
    pattern = re.compile(pat)
    hits = []
    for filepath in globlib.glob(path + "/**", recursive=True):
        try:
            for line_num, line in enumerate(open(filepath), 1):
                if pattern.search(line):
                    hits.append(f"{filepath}:{line_num}:{line.rstrip()}")
        except Exception:
            pass
    return "\n".join(hits[:50]) or "none"


def bash(cmd: str, timeout: float = 30):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=float(timeout), stdin=subprocess.DEVNULL,
        )
        out = (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") + (e.stderr or "")).strip()
        out += f"\n(timed out after {timeout:g}s)"
    if out:
        print(f"  {DIM}│ {out}{RESET}", flush=True)
    return out or "(empty)"


def _get_cron_manager():
    # Imported lazily to avoid package import cycles.
    from .crons import cron_manager
    return cron_manager


def cron(action: str, name: str = None, description: str = None,
         instructions: str = None, schedule: str = None, cron_id: str = None):
    cron_manager = _get_cron_manager()
    if action == "create":
        if not all([name, description, instructions, schedule]):
            return "error: create requires name, description, instructions, and schedule"
        try:
            cid = cron_manager.create_cron(name, description, instructions, schedule)
            return f"Cron created: {cid[:8]} ({name})"
        except ValueError as e:
            return f"error: invalid schedule -- {e}"

    elif action == "list":
        return cron_manager.list_crons()

    elif action == "edit":
        if not cron_id:
            return "error: edit requires cron_id"
        try:
            return cron_manager.edit_cron(
                cron_id, name=name, description=description,
                instructions=instructions, schedule=schedule,
            )
        except ValueError as e:
            return f"error: invalid schedule -- {e}"

    elif action == "delete":
        if not cron_id:
            return "error: delete requires cron_id"
        return cron_manager.delete_cron(cron_id)

    else:
        return f"error: unknown action '{action}'. Use create, list, edit, or delete."