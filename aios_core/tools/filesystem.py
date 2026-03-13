import os

from ..workspace import resolve_workspace_path


def read(path: str, offset: int = 0, limit: int = None):
    resolved = resolve_workspace_path(path)
    try:
        lines = open(resolved).readlines()
    except FileNotFoundError:
        return f"error: file not found: {resolved}"
    except IsADirectoryError:
        return f"error: path is a directory: {resolved}"
    if limit is None:
        limit = len(lines)
    selected = lines[offset : offset + limit]
    return "".join(f"{offset + idx + 1:4}| {line}" for idx, line in enumerate(selected))


def write(path: str, content: str):
    resolved = resolve_workspace_path(path)
    os.makedirs(os.path.dirname(str(resolved)), exist_ok=True)
    with open(resolved, "w") as f:
        f.write(content)
    return "ok"


def edit(path: str, old: str, new: str, all: bool = False):
    resolved = resolve_workspace_path(path)
    text = open(resolved).read()
    if old not in text:
        return "error: old_string not found"
    count = text.count(old)
    if not all and count > 1:
        return f"error: old_string appears {count} times, must be unique (use all=true)"
    replacement = text.replace(old, new) if all else text.replace(old, new, 1)
    with open(resolved, "w") as f:
        f.write(replacement)
    return "ok"
