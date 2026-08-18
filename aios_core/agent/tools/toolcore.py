"""Shared glue for the file/shell/process tools.

The heavy lifting lives in modules vendored byte-for-byte from hermes-agent:
ansi_strip, file_state, path_security, tool_output_limits, binary_extensions.
This module adds only the mini-aios-specific pieces: env-overridable limits,
hermes's terminal truncation/exit-code logic, and small local helpers.
"""

from __future__ import annotations

import difflib
import os
import re
import threading
import time
from pathlib import Path

from ..context import get_current_chat_id
from .ansi_strip import strip_ansi  # noqa: F401  (re-exported)
from .tool_output_limits import get_max_bytes, get_max_lines, get_max_line_length


def current_task_id() -> str:
    """Chat id for the vendored file_state registry (its task_id)."""
    return get_current_chat_id() or "default"


# ── Limits (AIOS_* env overrides on top of the vendored defaults) ────


def _env_int(name: str, fallback: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def max_output_chars() -> int:
    return _env_int("AIOS_TOOL_MAX_OUTPUT_CHARS", get_max_bytes())


def max_read_chars() -> int:
    return _env_int("AIOS_READ_MAX_CHARS", get_max_bytes())


def max_read_lines() -> int:
    return _env_int("AIOS_READ_MAX_LINES", get_max_lines())


def max_line_length() -> int:
    return _env_int("AIOS_READ_MAX_LINE_LENGTH", get_max_line_length())


# ── Truncation (hermes terminal_tool.py head/tail split) ─────────────


def truncate_middle(text: str, limit: int | None = None) -> str:
    """Cap text keeping 40% head (error messages often appear early) and
    60% tail (most recent/relevant output), with an explicit notice."""
    limit = limit or max_output_chars()
    if len(text) <= limit:
        return text
    head_chars = int(limit * 0.4)
    tail_chars = limit - head_chars
    omitted = len(text) - head_chars - tail_chars
    truncated_notice = (
        f"\n\n... [{omitted:,} chars omitted — output truncated to {limit:,} chars] ...\n\n"
    )
    return text[:head_chars] + truncated_notice + text[-tail_chars:]


def truncate_line(line: str, limit: int | None = None) -> str:
    limit = limit or max_line_length()
    if len(line) <= limit:
        return line
    return line[:limit] + "… [line truncated]"


# ── Binary / image / device detection ────────────────────────────────

IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".tif", ".heic",
}


def is_image_path(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def looks_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    # Control-char analysis on the DECODED text (hermes _is_likely_binary):
    # UTF-8 multibyte chars decode above the control range, so accented
    # text and BOMs don't false-positive the way raw-byte counting does.
    decoded = sample.decode("utf-8", errors="replace")
    non_printable = sum(1 for c in decoded[:1000] if ord(c) < 32 and c not in "\n\r\t")
    return non_printable / min(len(decoded), 1000) > 0.30


def is_blocked_device_path(path: str | Path) -> bool:
    """Paths that would block forever or produce infinite output."""
    resolved = str(path)
    return resolved.startswith(("/dev/", "/proc/", "/sys/")) or resolved in ("/dev", "/proc", "/sys")


# ── Line endings + BOM ───────────────────────────────────────────────

_BOM = "﻿"


def strip_bom(text: str) -> tuple[str, bool]:
    if text.startswith(_BOM):
        return text[len(_BOM):], True
    return text, False


def detect_line_ending(sample: str) -> str | None:
    crlf = sample.count("\r\n")
    lf = sample.count("\n") - crlf
    if crlf == 0 and lf == 0:
        return None
    return "\r\n" if crlf >= lf and crlf > 0 else "\n"


def normalize_line_endings(text: str, target: str) -> str:
    unified = text.replace("\r\n", "\n")
    return unified.replace("\n", target) if target == "\r\n" else unified


# ── Guard: refuse writing read()-tool display text as file content ───

_LINE_NUMBER_PREFIX_RE = re.compile(r"^\s*\d+\|")


def looks_like_line_numbered_output(content: str) -> bool:
    lines = [line for line in content.splitlines() if line.strip()]
    if len(lines) < 3:
        return False
    numbered = sum(bool(_LINE_NUMBER_PREFIX_RE.match(line)) for line in lines)
    return numbered / len(lines) >= 0.8


# ── Exit-code interpretation ─────────────────────────────────────────
# interpret_benign_exit_code is hermes terminal_tool._interpret_exit_code,
# copied verbatim: notes for non-zero exits that are NOT errors (grep with
# no matches, diff with differences). failure_exit_hint covers the generic
# error side (127 command-not-found, signal deaths).


def interpret_benign_exit_code(command: str, exit_code: int) -> str | None:
    """Return a human-readable note when a non-zero exit code is non-erroneous.

    Returns None when the exit code is 0 or genuinely signals an error.
    The note is appended to the tool result so the model doesn't waste
    turns investigating expected exit codes.
    """
    if exit_code == 0:
        return None

    # Extract the last command in a pipeline/chain — that determines the
    # exit code.  Handles  `cmd1 && cmd2`, `cmd1 | cmd2`, `cmd1; cmd2`.
    # Deliberately simple: split on shell operators and take the last piece.
    segments = re.split(r'\s*(?:\|\||&&|[|;])\s*', command)
    last_segment = (segments[-1] if segments else command).strip()

    # Get base command name (first word), stripping env var assignments
    # like  VAR=val cmd ...
    words = last_segment.split()
    base_cmd = ""
    for w in words:
        if "=" in w and not w.startswith("-"):
            continue  # skip VAR=val
        base_cmd = w.split("/")[-1]  # handle /usr/bin/grep -> grep
        break

    if not base_cmd:
        return None

    # Command-specific semantics
    semantics: dict[str, dict[int, str]] = {
        # grep/rg/ag/ack: 1=no matches found (normal), 2+=real error
        "grep":  {1: "No matches found (not an error)"},
        "egrep": {1: "No matches found (not an error)"},
        "fgrep": {1: "No matches found (not an error)"},
        "rg":    {1: "No matches found (not an error)"},
        "ag":    {1: "No matches found (not an error)"},
        "ack":   {1: "No matches found (not an error)"},
        # diff: 1=files differ (expected), 2+=real error
        "diff":  {1: "Files differ (expected, not an error)"},
        "colordiff": {1: "Files differ (expected, not an error)"},
        # find: 1=some dirs inaccessible but results may still be valid
        "find":  {1: "Some directories were inaccessible (partial results may still be valid)"},
        # test/[: 1=condition is false (expected)
        "test":  {1: "Condition evaluated to false (expected, not an error)"},
        "[":     {1: "Condition evaluated to false (expected, not an error)"},
        # curl: common non-error codes
        "curl":  {
            6: "Could not resolve host",
            7: "Failed to connect to host",
            22: "HTTP response code indicated error (e.g. 404, 500)",
            28: "Operation timed out",
        },
        # git: 1 is context-dependent but often normal (e.g. git diff with changes)
        "git":   {1: "Non-zero exit (often normal — e.g. 'git diff' returns 1 when files differ)"},
    }

    cmd_semantics = semantics.get(base_cmd)
    if cmd_semantics and exit_code in cmd_semantics:
        return cmd_semantics[exit_code]

    return None


_FAILURE_HINTS = {
    126: "found but not executable (check permissions)",
    127: "command not found (check spelling / PATH / installation)",
    130: "interrupted (SIGINT)",
}


def failure_exit_hint(exit_code: int) -> str | None:
    hint = _FAILURE_HINTS.get(exit_code)
    if hint:
        return hint
    if exit_code > 128:
        try:
            import signal as _signal

            return f"killed by signal {_signal.Signals(exit_code - 128).name}"
        except ValueError:
            return None
    return None


# ── Similar-file suggestions on not-found ────────────────────────────


def suggest_similar_files(resolved: Path, limit: int = 5) -> list[str]:
    parent = resolved.parent
    if not parent.is_dir():
        return []
    target = resolved.name.lower()
    stem = resolved.stem.lower()
    try:
        entries = [entry for entry in os.listdir(parent) if not entry.startswith(".")][:200]
    except OSError:
        return []

    scored: list[tuple[float, str]] = []
    for entry in entries:
        lower = entry.lower()
        if lower == target:
            score = 1.0
        elif Path(entry).stem.lower() == stem:
            score = 0.9  # same name, different extension
        elif lower.startswith(target) or target.startswith(lower):
            score = 0.7
        elif target in lower or (lower in target and len(lower) > 2):
            score = 0.6
        else:
            score = difflib.SequenceMatcher(None, target, lower).ratio()
            if score < 0.55:
                continue
        scored.append((score, str(parent / entry)))
    scored.sort(key=lambda item: -item[0])
    return [path for _, path in scored[:limit]]


# ── Repeat-call loop guard (hermes read/search tracker pattern) ──────
# Warns at 3 consecutive identical calls, hard-blocks at 4 — breaks the
# read/search loops that burn context without new information.

REPEAT_WARN_AT = 3
REPEAT_BLOCK_AT = 4

_repeat_lock = threading.Lock()
_repeat_state: dict[str, dict[str, object]] = {}


def track_repeat(key: tuple) -> int:
    """How many times in a row this exact call was made in the current chat
    context. Any different call resets the streak."""
    context = current_task_id()
    with _repeat_lock:
        state = _repeat_state.setdefault(context, {"last": None, "count": 0, "at": 0.0})
        if state["last"] == key:
            state["count"] = int(state["count"]) + 1
        else:
            state["last"] = key
            state["count"] = 1
        state["at"] = time.time()
        if len(_repeat_state) > 256:
            oldest = min(_repeat_state, key=lambda item: _repeat_state[item]["at"])
            _repeat_state.pop(oldest, None)
        return int(state["count"])


def repeat_notice(count: int, what: str) -> str | None:
    """Warning text for a repeated call, or None below the warn threshold.
    Callers should hard-block (return only this text) at REPEAT_BLOCK_AT."""
    if count >= REPEAT_BLOCK_AT:
        return (
            f"BLOCKED: this exact {what} was repeated {count} times in a row with "
            "unchanged results. You already have this information — stop repeating "
            "the call and proceed with the task."
        )
    if count >= REPEAT_WARN_AT:
        return (
            f"note: this exact {what} was repeated {count} times in a row. "
            "The result has not changed — use the information you already have."
        )
    return None
