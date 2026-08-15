from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from ..runtime_context import resolve_chat_files_path
from ..workspace import PathAccessError
from . import file_state
from .binary_extensions import has_binary_extension
from .toolcore import (
    current_task_id,
    detect_line_ending,
    is_blocked_device_path,
    is_image_path,
    looks_binary,
    looks_like_line_numbered_output,
    max_read_chars,
    max_read_lines,
    normalize_line_endings,
    repeat_notice,
    strip_bom,
    suggest_similar_files,
    track_repeat,
    truncate_line,
)

_BOM = "﻿"


def _guard_path(resolved: Path, *, for_read: bool) -> str | None:
    if is_blocked_device_path(resolved):
        return f"error: {resolved} is a device/system path that would block or produce infinite output"
    if resolved.is_dir():
        entries = ""
        if for_read:
            try:
                names = sorted(os.listdir(resolved))[:20]
                entries = "\nfirst entries:\n" + "\n".join(f"  {name}" for name in names)
            except OSError:
                pass
        return f"error: path is a directory: {resolved}{entries}"
    return None


def _not_found_error(resolved: Path) -> str:
    suggestions = suggest_similar_files(resolved)
    message = f"error: file not found: {resolved}"
    if suggestions:
        message += "\ndid you mean:\n" + "\n".join(f"  {path}" for path in suggestions)
    return message


def _read_text(resolved: Path) -> tuple[str, bool] | str:
    """Return (text, had_bom) or an error string for binary/unreadable files."""
    if has_binary_extension(str(resolved)):
        kind = "image" if is_image_path(resolved) else "binary"
        hint = " Use show_canvas to display it in the chat." if kind == "image" else ""
        try:
            size = resolved.stat().st_size
        except OSError:
            size = 0
        return f"error: {resolved} is a {kind} file ({size:,} bytes) — cannot display as text.{hint}"
    try:
        raw = resolved.read_bytes()
    except FileNotFoundError:
        return _not_found_error(resolved)
    except OSError as exc:
        return f"error: cannot read {resolved}: {exc}"
    if looks_binary(raw[:1024]):
        return f"error: {resolved} contains binary data ({len(raw):,} bytes) — cannot display as text"
    text, had_bom = strip_bom(raw.decode("utf-8", errors="replace"))
    return text, had_bom


def _atomic_write(resolved: Path, content: str) -> None:
    """Temp file in the target's own directory + rename, so a crash or full
    disk never leaves a partially-written target. Preserves existing mode."""
    parent = resolved.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".aios-tmp.", dir=str(parent))
    try:
        if resolved.exists():
            try:
                os.chmod(tmp, os.stat(resolved).st_mode & 0o7777)
            except OSError:
                pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, resolved)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _stale_warnings(resolved: Path) -> list[str]:
    """Advisory warnings before overwriting: cross-run staleness from the
    vendored registry, plus a blind-overwrite check it doesn't cover."""
    task_id = current_task_id()
    warnings = []
    stale = file_state.check_stale(task_id, str(resolved))
    if stale:
        warnings.append(f"warning: {stale}")
    elif resolved.exists() and str(resolved) not in file_state.known_reads(task_id):
        warnings.append(
            f"warning: {resolved} exists but was not read in this session — "
            "overwriting content you have not seen. Read it first if unsure."
        )
    return warnings


def read(path: str, offset: int = 0, limit: int = None):
    """Read a text file with line numbers.

    Args:
        path: File path (relative paths resolve to the shared applications dir).
        offset: 0-based line to start from (default 0).
        limit: Max lines to return (default 2000).
    """
    try:
        resolved = resolve_chat_files_path(path)
    except PathAccessError as exc:
        return f"error: {exc}"
    guard = _guard_path(resolved, for_read=True)
    if guard:
        return guard
    if not resolved.exists():
        return _not_found_error(resolved)

    if has_binary_extension(str(resolved)):
        kind = "image" if is_image_path(resolved) else "binary"
        hint = " Use show_canvas to display it in the chat." if kind == "image" else ""
        try:
            size = resolved.stat().st_size
        except OSError:
            size = 0
        return f"error: {resolved} is a {kind} file ({size:,} bytes) — cannot display as text.{hint}"
    try:
        with open(resolved, "rb") as probe:
            if looks_binary(probe.read(1024)):
                size = resolved.stat().st_size
                return f"error: {resolved} contains binary data ({size:,} bytes) — cannot display as text"
    except FileNotFoundError:
        return _not_found_error(resolved)
    except OSError as exc:
        return f"error: cannot read {resolved}: {exc}"

    offset = max(int(offset or 0), 0)
    limit = int(limit) if limit else max_read_lines()
    if limit <= 0:
        limit = max_read_lines()

    count = track_repeat(("read", str(resolved), offset, limit))
    notice = repeat_notice(count, "read")
    if notice and notice.startswith("BLOCKED"):
        return notice

    # Stream line-by-line: constant memory on arbitrarily large files
    # (utf-8-sig transparently drops a leading BOM when present).
    selected: list[str] = []
    total = 0
    try:
        with open(resolved, "r", encoding="utf-8-sig", errors="replace") as handle:
            for idx, line in enumerate(handle):
                total += 1
                if offset <= idx < offset + limit:
                    selected.append(line)
    except OSError as exc:
        return f"error: cannot read {resolved}: {exc}"

    if offset >= total and total > 0:
        return f"error: offset {offset} is beyond end of file ({total} lines)"

    rendered = "".join(
        f"{offset + idx + 1:4}| {truncate_line(line.rstrip(chr(10)).rstrip(chr(13)))}\n"
        for idx, line in enumerate(selected)
    )

    if len(rendered) > max_read_chars():
        return (
            f"error: read produced {len(rendered):,} characters, over the "
            f"{max_read_chars():,} char limit. The file has {total} lines — "
            "use offset and limit to read a smaller range."
        )

    end = offset + len(selected)
    file_state.record_read(
        current_task_id(), str(resolved), partial=(offset > 0 or end < total)
    )

    if end < total:
        rendered += (
            f"\n(showing lines {offset + 1}-{end} of {total} — "
            f"continue with offset={end})"
        )
    if notice:
        rendered += f"\n({notice})"
    return rendered or "(empty file)"


def write(path: str, content: str):
    """Write content to a file (atomic: temp file + rename).

    Args:
        path: File path (relative paths resolve to the shared applications dir).
        content: Full file content to write.
    """
    try:
        resolved = resolve_chat_files_path(path, for_write=True)
    except PathAccessError as exc:
        return f"error: {exc}"
    guard = _guard_path(resolved, for_read=False)
    if guard:
        return guard
    if looks_like_line_numbered_output(content):
        return (
            "error: content looks like read-tool output (line-number prefixes like "
            "' 12| ...'). Strip the prefixes and write the real file content."
        )

    warnings = _stale_warnings(resolved)
    if "\x1b" in content:
        warnings.append(
            "warning: content contains raw ANSI escape bytes — if these leaked from "
            "terminal output, rewrite the file without them"
        )

    with file_state.lock_path(str(resolved)):
        try:
            _atomic_write(resolved, content)
        except OSError as exc:
            return f"error: cannot write {resolved}: {exc}"
        file_state.note_write(current_task_id(), str(resolved))

    lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    result = f"ok: wrote {len(content.encode('utf-8')):,} bytes ({lines} lines) to {resolved}"
    for warning in warnings:
        result += f"\n{warning}"
    return result


def _whitespace_collapsed(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def edit(path: str, old: str, new: str, all: bool = False):
    """Replace old with new in a file (old must match exactly and be unique
    unless all=true). Line endings of the file are preserved.

    Args:
        path: File path (relative paths resolve to the shared applications dir).
        old: Exact text to replace.
        new: Replacement text.
        all: Replace every occurrence instead of requiring uniqueness.
    """
    try:
        resolved = resolve_chat_files_path(path, for_write=True)
    except PathAccessError as exc:
        return f"error: {exc}"
    guard = _guard_path(resolved, for_read=False)
    if guard:
        return guard
    if not resolved.exists():
        return _not_found_error(resolved)
    if old == new:
        return "error: old and new are identical"

    with file_state.lock_path(str(resolved)):
        loaded = _read_text(resolved)
        if isinstance(loaded, str):
            return loaded
        text, had_bom = loaded

        warnings = _stale_warnings(resolved)

        match_old, match_new = old, new
        if match_old not in text:
            # Retry with the file's dominant line ending — the most common
            # mismatch is CRLF files edited with LF strings.
            ending = detect_line_ending(text)
            if ending == "\r\n" and "\r\n" not in match_old:
                match_old = normalize_line_endings(old, "\r\n")
                match_new = normalize_line_endings(new, "\r\n")

        if match_old not in text:
            if _whitespace_collapsed(old) and _whitespace_collapsed(old) in _whitespace_collapsed(text):
                return (
                    "error: old_string not found exactly, but a match exists with "
                    "different whitespace/indentation. Re-read the file and copy the "
                    "exact text including indentation."
                )
            return "error: old_string not found in file"

        occurrences = text.count(match_old)
        if not all and occurrences > 1:
            return f"error: old_string appears {occurrences} times, must be unique (use all=true)"

        replacement = (
            text.replace(match_old, match_new)
            if all
            else text.replace(match_old, match_new, 1)
        )
        if had_bom:
            replacement = _BOM + replacement
        try:
            _atomic_write(resolved, replacement)
        except OSError as exc:
            return f"error: cannot write {resolved}: {exc}"
        file_state.note_write(current_task_id(), str(resolved))

    replaced = occurrences if all else 1
    result = f"ok: replaced {replaced} occurrence{'s' if replaced != 1 else ''} in {resolved}"
    for warning in warnings:
        result += f"\n{warning}"
    return result
