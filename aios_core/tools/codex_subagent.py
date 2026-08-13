"""Codex CLI exposed as a streaming subagent.

The blocking ``codex`` tool shells out to ``codex exec`` and returns one final
string — the harness sees nothing until it finishes. ``codex_subagent`` runs
the same CLI with ``--json`` and translates its JSONL event stream into the
shared ``SubagentStreamEvent`` protocol, so the main harness renders Codex's
commands and file edits live (like the in-process ``subagent`` worker), then
still returns Codex's final message as the tool result.

The JSONL translation lives in :func:`translate_codex_event`, kept pure and
free of process/IO concerns so it can be unit-tested against real Codex output
(see tests/fixtures/codex_jsonl) without touching the streaming machinery.

Real Codex (`codex exec --json`, v0.147) emits top-level events
``thread.started`` / ``turn.started`` / ``item.started`` / ``item.completed`` /
``turn.completed``. The interesting payload is ``item`` with a ``type`` of
``agent_message`` (has ``text``), ``command_execution`` (``command``,
``aggregated_output``, ``exit_code``), or ``file_change`` (``changes``).
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from time import monotonic
from typing import Any, Iterator
from uuid import uuid4

from ..runtime_context import resolve_chat_files_path
from .subagent_events import SubagentStreamEvent, build_subagent_stream_event


def _summarize_input(item: dict[str, Any]) -> str:
    itype = item.get("type")
    if itype == "command_execution":
        return str(item.get("command") or "")
    if itype == "file_change":
        parts = []
        for change in item.get("changes") or []:
            path = change.get("path") or ""
            parts.append(f"{change.get('kind', 'edit')} {os.path.basename(path)}".strip())
        return ", ".join(parts) or "file change"
    # Best-effort for unknown item types (reasoning, web_search, mcp_tool_call...).
    return str(item.get("command") or item.get("query") or item.get("name") or itype or "tool")


def _summarize_output(item: dict[str, Any]) -> str:
    itype = item.get("type")
    if itype == "command_execution":
        out = str(item.get("aggregated_output") or "").strip()
        exit_code = item.get("exit_code")
        if exit_code is not None:
            return f"{out}\n(exit {exit_code})".strip()
        return out or "(no output)"
    if itype == "file_change":
        summary = _summarize_input(item)
        status = item.get("status")
        return f"{summary} ({status})" if status else summary
    return str(item.get("status") or "done")


def translate_codex_event(obj: Any) -> list[dict[str, Any]]:
    """Map one parsed Codex JSONL object to normalized event descriptors.

    Descriptor ``kind`` is one of ``text``, ``tool_start``, ``tool_end``.
    Uninteresting events (thread/turn lifecycle, unknown shapes) map to ``[]``.
    Kept pure so it is trivially testable against captured fixtures.
    """
    if not isinstance(obj, dict):
        return []

    event_type = obj.get("type")
    item = obj.get("item") if isinstance(obj.get("item"), dict) else None

    if event_type == "item.started" and item is not None:
        if item.get("type") == "agent_message":
            return []  # message text is only final on item.completed
        return [
            {
                "kind": "tool_start",
                "tool_call_id": str(item.get("id") or uuid4()),
                "tool_name": str(item.get("type") or "tool"),
                "input": _summarize_input(item),
            }
        ]

    if event_type == "item.completed" and item is not None:
        if item.get("type") == "agent_message":
            text = item.get("text")
            return [{"kind": "text", "value": str(text)}] if text else []
        return [
            {
                "kind": "tool_end",
                "tool_call_id": str(item.get("id") or uuid4()),
                "tool_name": str(item.get("type") or "tool"),
                "output": _summarize_output(item),
            }
        ]

    # thread.started, turn.started, turn.completed, and anything unrecognized.
    return []


def _reader_thread(stream: Any, out_queue: "queue.Queue[str | None]") -> None:
    try:
        for line in iter(stream.readline, ""):
            out_queue.put(line)
    finally:
        out_queue.put(None)  # sentinel: stream closed


def codex_subagent(
    task: str | None = None,
    timeout: float = 180,
    model: str | None = None,
    path: str = ".",
    fc=None,
) -> Iterator[SubagentStreamEvent | str]:
    """Delegate a coding task to Codex, streaming its commands and edits.

    Runs the Codex CLI as a subagent: it streams Codex's live tool activity to
    the harness and returns Codex's final message when done. Use this to hand
    off self-contained coding work (implement/edit/refactor in a directory).
    Give ``task`` as a complete, self-contained instruction including the target
    files — Codex cannot see the chat. ``path`` is the working directory.
    """
    if not isinstance(task, str) or not task.strip():
        yield "error: task is required"
        return
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        yield "error: timeout must be a number"
        return
    if timeout_value <= 0:
        yield "error: timeout must be > 0"
        return
    if not isinstance(path, str) or not path.strip():
        yield "error: path must be a non-empty string"
        return

    workdir = resolve_chat_files_path(path.strip())
    if not workdir.exists():
        yield f"error: path does not exist: {workdir}"
        return
    if not workdir.is_dir():
        yield f"error: path is not a directory: {workdir}"
        return

    cmd = [
        "codex",
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--sandbox",
        "danger-full-access",
    ]
    if isinstance(model, str) and model.strip():
        cmd.extend(["--model", model.strip()])
    cmd.append(task.strip())

    child_run_id = str(uuid4())
    parent_tool_call_id = str(getattr(fc, "call_id", None) or child_run_id)

    def event(child_event_type: str, **kwargs: Any) -> SubagentStreamEvent:
        return build_subagent_stream_event(
            parent_tool_call_id=parent_tool_call_id,
            child_run_id=child_run_id,
            child_event_type=child_event_type,
            **kwargs,
        )

    text_chunks: list[str] = []
    final_message: str | None = None

    yield event("stream_start")

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(workdir),
        )
    except FileNotFoundError:
        yield event("stream_error", error="codex CLI is not installed or not on PATH")
        yield "error: codex CLI is not installed or not on PATH"
        return
    except Exception as exc:
        yield event("stream_error", error=str(exc))
        yield f"error: codex failed -- {exc}"
        return

    line_queue: "queue.Queue[str | None]" = queue.Queue()
    reader = threading.Thread(
        target=_reader_thread, args=(process.stdout, line_queue), daemon=True
    )
    reader.start()
    deadline = monotonic() + timeout_value

    def _drain(descriptors: list[dict[str, Any]]) -> Iterator[SubagentStreamEvent]:
        nonlocal final_message
        for desc in descriptors:
            kind = desc["kind"]
            if kind == "text":
                text_chunks.append(desc["value"])
                final_message = desc["value"]  # last agent_message == the answer
            elif kind == "tool_start":
                yield event(
                    "tool_call_start",
                    tool_call_id=desc["tool_call_id"],
                    tool_name=desc["tool_name"],
                    input=desc["input"],
                )
            elif kind == "tool_end":
                yield event(
                    "tool_call_end",
                    tool_call_id=desc["tool_call_id"],
                    tool_name=desc["tool_name"],
                    output=desc["output"],
                )

    try:
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                line = line_queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if process.poll() is not None and line_queue.empty():
                    break
                continue

            if line is None:  # reader hit EOF
                break

            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError:
                text_chunks.append(stripped)  # non-JSON output -> content fallback
                continue
            for out_event in _drain(translate_codex_event(obj)):
                yield out_event
    except TimeoutError:
        process.kill()
        yield event("stream_error", error=f"Codex timed out after {timeout_value:g}s.")
        yield f"error: codex timed out after {timeout_value:g}s"
        return

    returncode = process.wait()

    if returncode != 0:
        stderr_output = (process.stderr.read() if process.stderr else "") or ""
        detail = stderr_output.strip() or "".join(text_chunks).strip()
        yield event("stream_error", error=detail or f"codex exit {returncode}")
        if detail:
            yield f"error: codex exit {returncode} -- {detail}"
        else:
            yield f"error: codex exit {returncode}"
        return

    yield event("stream_end")
    result = final_message or "".join(text_chunks).strip()
    yield result or "(empty)"
