import os
import re
import glob as globlib

from ..workspace import resolve_workspace_path


def glob(pat: str, path: str = "."):
    resolved_path = str(resolve_workspace_path(path))
    pattern = (resolved_path + "/" + pat).replace("//", "/")
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
    resolved_path = str(resolve_workspace_path(path))
    for filepath in globlib.glob(resolved_path + "/**", recursive=True):
        try:
            for line_num, line in enumerate(open(filepath), 1):
                if pattern.search(line):
                    hits.append(f"{filepath}:{line_num}:{line.rstrip()}")
        except Exception:
            pass
    return "\n".join(hits[:50]) or "none"
