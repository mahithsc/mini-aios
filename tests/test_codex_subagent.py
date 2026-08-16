"""Tier 1 (mocked, deterministic) + Tier 2 (live, guarded) tests for the
codex_subagent tool.

Tier 1 pins the mechanics with no model/network: the pure JSONL->event
translation (against REAL captured Codex output in tests/fixtures/codex_jsonl)
and the streaming generator's event ordering, timeout, error, and validation
behavior with a mocked subprocess.

Tier 2 runs the real Codex CLI end-to-end; it is skipped unless CODEX_LIVE_TEST
is set, so `make test` stays fast and deterministic.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import pytest
from agents.tool_context import ToolContext

from aios_core.openai_runtime import AgentRuntimeContext, as_function_tool
from aios_core.tools.codex_subagent import codex_subagent, translate_codex_event
from aios_core.tools.subagent_events import SubagentStreamEvent

FIXTURES = Path(__file__).parent / "fixtures" / "codex_jsonl"


def _load_jsonl(name: str) -> list[dict]:
    return [
        json.loads(line)
        for line in (FIXTURES / name).read_text().splitlines()
        if line.strip()
    ]


def _collect(gen) -> tuple[list[SubagentStreamEvent], str]:
    """Drain the tool generator into (stream_events, final_result_string)."""
    events: list[SubagentStreamEvent] = []
    final = ""
    for item in gen:
        if isinstance(item, SubagentStreamEvent):
            events.append(item)
        else:
            final = item  # last string is the tool result
    return events, final


def _types(events: list[SubagentStreamEvent]) -> list[str]:
    return [getattr(e, "child_event_type", None) for e in events]


# --------------------------------------------------------------------------- #
# Tier 1a: pure translation against real captured Codex JSONL
# --------------------------------------------------------------------------- #


def test_translate_agent_message_yields_text():
    obj = {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "hi there"}}
    assert translate_codex_event(obj) == [{"kind": "text", "value": "hi there"}]


def test_translate_command_start_and_end():
    events = _load_jsonl("command_read.jsonl")
    starts = [d for o in events for d in translate_codex_event(o) if d["kind"] == "tool_start"]
    ends = [d for o in events for d in translate_codex_event(o) if d["kind"] == "tool_end"]
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0]["tool_name"] == "command_execution"
    assert "sample.txt" in starts[0]["input"]
    assert starts[0]["tool_call_id"] == ends[0]["tool_call_id"]  # correlated by item id
    assert "hello world" in ends[0]["output"]
    assert "exit 0" in ends[0]["output"]


def test_translate_file_change_summarizes_paths():
    events = _load_jsonl("file_change.jsonl")
    starts = [d for o in events for d in translate_codex_event(o) if d["kind"] == "tool_start"]
    assert any(s["tool_name"] == "file_change" and "poem.md" in s["input"] for s in starts)


def test_translate_ignores_thread_and_turn_events():
    for t in ("thread.started", "turn.started", "turn.completed"):
        assert translate_codex_event({"type": t}) == []


def test_translate_last_agent_message_is_final_answer():
    events = _load_jsonl("command_read.jsonl")
    texts = [d["value"] for o in events for d in translate_codex_event(o) if d["kind"] == "text"]
    assert len(texts) >= 1
    assert texts[-1].strip()  # last agent_message is non-empty -> becomes tool result


# --------------------------------------------------------------------------- #
# Mocked subprocess plumbing
# --------------------------------------------------------------------------- #


class _FakeStdout:
    def __init__(self, lines: list[str]):
        self._lines = list(lines)

    def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""  # "" == EOF


class _BlockingStdout:
    def __init__(self, released: threading.Event):
        self._released = released

    def readline(self) -> str:
        self._released.wait()  # block until the process is "killed"
        return ""


class _FakeStderr:
    def __init__(self, text: str = ""):
        self._text = text

    def read(self) -> str:
        return self._text


class _FakePopen:
    def __init__(self, *, lines=None, returncode=0, stderr_text="", block=None):
        self.stdout = _BlockingStdout(block) if block is not None else _FakeStdout(lines or [])
        self.stderr = _FakeStderr(stderr_text)
        self._returncode = returncode
        self._block = block
        self.killed = False

    def poll(self):
        return -9 if self.killed else None

    def wait(self):
        return self._returncode

    def kill(self):
        self.killed = True
        if self._block is not None:
            self._block.set()


def _patch_popen(monkeypatch, popen):
    monkeypatch.setattr(
        "aios_core.tools.codex_subagent.subprocess.Popen",
        lambda *a, **k: popen,
    )


@pytest.fixture
def valid_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aios_core.tools.codex_subagent.resolve_chat_files_path",
        lambda p: tmp_path,
    )
    return tmp_path


# --------------------------------------------------------------------------- #
# Tier 1b: streaming generator behavior (mocked Popen)
# --------------------------------------------------------------------------- #


def test_generator_happy_path_orders_events_and_returns_last_message(valid_path, monkeypatch):
    lines = [line + "\n" for line in (FIXTURES / "command_read.jsonl").read_text().splitlines() if line.strip()]
    _patch_popen(monkeypatch, _FakePopen(lines=lines, returncode=0))

    events, final = _collect(codex_subagent(task="read the file", timeout=30, path="."))

    types = _types(events)
    assert types[0] == "stream_start"
    assert types[-1] == "stream_end"
    assert "tool_call_start" in types and "tool_call_end" in types
    # command_execution start precedes its end
    assert types.index("tool_call_start") < types.index("tool_call_end")
    assert final and "hello world".lower() in final.lower() or final.strip()  # last agent_message


def test_generator_timeout_kills_process_and_errors(valid_path, monkeypatch):
    block = threading.Event()
    popen = _FakePopen(block=block)
    _patch_popen(monkeypatch, popen)

    events, final = _collect(codex_subagent(task="hang", timeout=0.3, path="."))

    assert "stream_error" in _types(events)
    assert "timed out" in final.lower()
    assert popen.killed is True


def test_adapter_cancellation_kills_codex_process(valid_path, monkeypatch):
    block = threading.Event()
    started = threading.Event()
    popen = _FakePopen(block=block)
    monkeypatch.setattr(
        "aios_core.tools.codex_subagent.subprocess.Popen",
        lambda *args, **kwargs: (started.set() or popen),
    )

    async def invoke() -> list[str]:
        nested_events: list[str] = []

        async def sink(event) -> None:
            nested_events.append(event.child_event_type)

        runtime = AgentRuntimeContext(event_sink=sink)
        runtime.bind_to_current_loop()
        tool = as_function_tool(codex_subagent)
        context = ToolContext(
            context=runtime,
            tool_name="codex_subagent",
            tool_call_id="parent",
            tool_arguments="{}",
        )
        task = asyncio.create_task(
            tool.on_invoke_tool(
                context,
                '{"task":"work","timeout":600,"model":null,"path":"."}',
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(block.wait, 1)
        return nested_events

    assert asyncio.run(invoke()) == ["stream_start"]
    assert popen.killed is True


def test_generator_nonzero_exit_reports_error(valid_path, monkeypatch):
    _patch_popen(monkeypatch, _FakePopen(lines=[], returncode=1, stderr_text="boom"))

    events, final = _collect(codex_subagent(task="fail", timeout=30, path="."))

    assert "stream_error" in _types(events)
    assert final.startswith("error:")
    assert "boom" in final


def test_generator_codex_missing_is_clean_error(valid_path, monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr("aios_core.tools.codex_subagent.subprocess.Popen", _raise)
    events, final = _collect(codex_subagent(task="x", timeout=30, path="."))
    assert final.startswith("error:")
    assert "not installed" in final or "not on PATH" in final


@pytest.mark.parametrize(
    "kwargs, needle",
    [
        ({"task": "", "path": "."}, "task is required"),
        ({"task": None, "path": "."}, "task is required"),
        ({"task": "x", "timeout": 0, "path": "."}, "timeout"),
        ({"task": "x", "timeout": "nope", "path": "."}, "timeout"),
    ],
)
def test_validation_rejects_bad_args(valid_path, kwargs, needle):
    _, final = _collect(codex_subagent(**kwargs))
    assert final.startswith("error:")
    assert needle in final


def test_validation_rejects_missing_path(tmp_path, monkeypatch):
    missing = tmp_path / "nope"
    monkeypatch.setattr(
        "aios_core.tools.codex_subagent.resolve_chat_files_path", lambda p: missing
    )
    _, final = _collect(codex_subagent(task="x", timeout=30, path="nope"))
    assert final.startswith("error:")
    assert "does not exist" in final


# --------------------------------------------------------------------------- #
# Tier 2: live end-to-end (guarded)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    not os.getenv("CODEX_LIVE_TEST"),
    reason="set CODEX_LIVE_TEST=1 to run the live Codex end-to-end test",
)
def test_live_codex_subagent_generates_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aios_core.tools.codex_subagent.resolve_chat_files_path", lambda p: tmp_path
    )
    events, final = _collect(
        codex_subagent(
            task="Create a file hello.txt whose only contents are the word hi.",
            timeout=180,
            path=".",
        )
    )
    assert (tmp_path / "hello.txt").exists()
    assert (tmp_path / "hello.txt").read_text().strip().lower().startswith("hi")
    assert "tool_call_end" in _types(events)
    assert final.strip()
