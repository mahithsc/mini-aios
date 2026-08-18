from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import glob as globlib
from pathlib import Path

from ..context import resolve_agent_path
from .toolcore import (
    looks_binary,
    repeat_notice,
    strip_ansi,
    track_repeat,
    truncate_middle,
)

_NOISE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", ".cache", ".tox",
}
_MAX_GLOB_RESULTS = 200
_MAX_MATCH_LINE_CHARS = 500
_RG_TIMEOUT = 30

_rg_path: str | None | bool = False  # False = not probed yet


def _ripgrep() -> str | None:
    global _rg_path
    if _rg_path is False:
        _rg_path = shutil.which("rg")
    return _rg_path


def _is_noise(path: str) -> bool:
    return bool(_NOISE_DIRS.intersection(Path(path).parts))


def glob(pat: str, path: str = "."):
    """Find files matching a glob pattern, newest first.

    Args:
        pat: Glob pattern, e.g. "**/*.py" (recursive) or "*.md".
        path: Directory to search (default: chat scratch).
    """
    resolved_path = resolve_agent_path(path)
    if not resolved_path.exists():
        return f"error: path does not exist: {resolved_path}"
    pattern = (str(resolved_path) + "/" + pat).replace("//", "/")
    try:
        files = globlib.glob(pattern, recursive=True)
    except re.error as exc:
        return f"error: invalid glob pattern: {exc}"

    # Hide dependency/VCS noise unless the pattern explicitly targets it.
    if not _NOISE_DIRS.intersection(Path(pat).parts):
        files = [file for file in files if not _is_noise(file)]

    files = sorted(
        files,
        key=lambda f: os.path.getmtime(f) if os.path.isfile(f) else 0,
        reverse=True,
    )
    total = len(files)
    shown = files[:_MAX_GLOB_RESULTS]
    lines = [file + ("/" if os.path.isdir(file) else "") for file in shown]
    result = "\n".join(lines) or "none"
    if total > len(shown):
        result += f"\n({total - len(shown)} more matches not shown — narrow the pattern)"
    return result


def _grep_rg(
    pattern: str, resolved: Path, file_glob: str | None, context: int
) -> tuple[list[str], str | None]:
    """Run ripgrep; returns (output_lines, error). Exit 1 = no matches."""
    cmd = [
        _ripgrep(), "--line-number", "--no-heading", "--with-filename",
        "--color=never", "--max-columns", str(_MAX_MATCH_LINE_CHARS),
    ]
    if context > 0:
        cmd += ["-C", str(context)]
    if file_glob:
        cmd += ["--glob", file_glob]
    cmd += ["-e", pattern, str(resolved)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_RG_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return [], f"error: search timed out after {_RG_TIMEOUT}s — narrow the path or pattern"
    if proc.returncode == 2 and not proc.stdout.strip():
        detail = (proc.stderr or "").strip().splitlines()
        return [], "error: search failed: " + (detail[-1] if detail else "unknown ripgrep error")
    lines = [line for line in strip_ansi(proc.stdout).splitlines() if line]
    return lines, None


def _grep_python(
    pattern: re.Pattern, resolved: Path, file_glob: str | None, context: int
) -> list[str]:
    """Pure-Python fallback mirroring the rg output format (file:line:content)."""
    if resolved.is_file():
        candidates = [resolved]
    else:
        candidates = []
        for root, dirs, names in os.walk(resolved):
            dirs[:] = [d for d in dirs if d not in _NOISE_DIRS and not d.startswith(".")]
            candidates.extend(Path(root) / name for name in names)

    output: list[str] = []
    for filepath in candidates:
        if file_glob and not fnmatch.fnmatch(filepath.name, file_glob):
            continue
        try:
            raw = filepath.read_bytes()
        except OSError:
            continue
        if looks_binary(raw[:1024]):
            continue
        lines = raw.decode("utf-8", errors="replace").splitlines()
        hit_indexes = [idx for idx, line in enumerate(lines) if pattern.search(line)]
        if not hit_indexes:
            continue
        shown: set[int] = set()
        for hit in hit_indexes:
            start = max(hit - context, 0)
            end = min(hit + context, len(lines) - 1)
            for idx in range(start, end + 1):
                if idx in shown:
                    continue
                shown.add(idx)
                separator = ":" if idx == hit else "-"
                content = lines[idx][:_MAX_MATCH_LINE_CHARS]
                output.append(f"{filepath}{separator}{idx + 1}{separator}{content}")
        if len(output) > 10_000:  # hard safety valve before paging
            break
    return output


def grep(
    pat: str,
    path: str = ".",
    glob: str = None,
    context: int = 0,
    limit: int = 50,
    offset: int = 0,
):
    """Search file contents for a regex pattern. Uses ripgrep when available
    (respects .gitignore); falls back to a pure-Python scan.

    Args:
        pat: Regex pattern to search for.
        path: File or directory to search (default: chat scratch).
        glob: Optional filename filter, e.g. "*.py".
        context: Lines of context around each match (default 0).
        limit: Max output lines to return (default 50, max 200).
        offset: Output line to start from, for paging (default 0).
    """
    try:
        compiled = re.compile(pat)
    except re.error as exc:
        return f"error: invalid regex pattern: {exc}"

    resolved = resolve_agent_path(path)
    if not resolved.exists():
        return f"error: path does not exist: {resolved}"

    context = max(int(context or 0), 0)
    limit = min(max(int(limit or 50), 1), 200)
    offset = max(int(offset or 0), 0)

    count = track_repeat(("grep", pat, str(resolved), glob or "", context, limit, offset))
    notice = repeat_notice(count, "search")
    if notice and notice.startswith("BLOCKED"):
        return notice

    if _ripgrep():
        lines, error = _grep_rg(pat, resolved, glob, context)
        if error:
            return error
    else:
        lines = _grep_python(compiled, resolved, glob, context)

    total = len(lines)
    if total == 0:
        return "none"

    page = [line[: _MAX_MATCH_LINE_CHARS + 120] for line in lines[offset : offset + limit]]
    result = "\n".join(page)
    if offset > 0 or total > offset + len(page):
        result += (
            f"\n(showing lines {offset + 1}-{offset + len(page)} of {total}"
            + (f" — continue with offset={offset + len(page)}" if total > offset + len(page) else "")
            + " — narrow with a more specific pattern or glob)"
        )
    if notice:
        result += f"\n({notice})"
    return truncate_middle(result)
