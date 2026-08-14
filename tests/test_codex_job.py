"""Async Codex job (codex_start / codex_poll / codex_stop) tests.

Mocked subprocess, no network: a fake Codex process streams REAL captured JSONL
(tests/fixtures/codex_jsonl) into a background CodexJob, and we assert the
start -> poll lifecycle, event streaming, stop, error, and validation paths.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from aios_core.tools.codex_job import CodexJobManager

FIXTURES = Path(__file__).parent / "fixtures" / "codex_jsonl"


class _Stdout:
    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else ""


class _BlockingStdout:
    def __init__(self, released: threading.Event):
        self._released = released

    def readline(self):
        self._released.wait()
        return ""


class _Stderr:
    def __init__(self, text=""):
        self._text = text

    def read(self):
        return self._text


class _FakePopen:
    def __init__(self, *, lines=None, returncode=0, stderr_text="", block=None):
        self.stdout = _BlockingStdout(block) if block is not None else _Stdout(lines or [])
        self.stderr = _Stderr(stderr_text)
        self._rc = returncode
        self._block = block
        self.killed = False

    def poll(self):
        return self._rc if self.killed else None

    def wait(self):
        return self._rc

    def kill(self):
        self.killed = True
        if self._block is not None:
            self._block.set()


def _patch(monkeypatch, popen):
    monkeypatch.setattr("aios_core.tools.codex_job.subprocess.Popen", lambda *a, **k: popen)


@pytest.fixture
def valid_path(tmp_path, monkeypatch):
    monkeypatch.setattr("aios_core.tools.codex_job.resolve_chat_files_path", lambda p: tmp_path)
    return tmp_path


def _wait_done(mgr, job_id, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        res = mgr.poll(job_id, wait=0.5)
        if res.get("status") != "running":
            return res
    return mgr.poll(job_id)


def _fixture_lines(name):
    return [l + "\n" for l in (FIXTURES / name).read_text().splitlines() if l.strip()]


def test_start_returns_job_id_immediately(valid_path, monkeypatch):
    _patch(monkeypatch, _FakePopen(lines=_fixture_lines("command_read.jsonl")))
    mgr = CodexJobManager()
    started = mgr.start("read the file", path=".")
    assert started.get("status") == "running"
    assert "job_id" in started


def test_poll_reaches_done_with_result_and_events(valid_path, monkeypatch):
    _patch(monkeypatch, _FakePopen(lines=_fixture_lines("command_read.jsonl"), returncode=0))
    mgr = CodexJobManager()
    started = mgr.start("read the file", path=".")
    res = _wait_done(mgr, started["job_id"])
    assert res["status"] == "done"
    assert res["result"] and res["result"].strip()
    kinds = [e["kind"] for e in res["events"]]
    assert "tool_start" in kinds and "tool_end" in kinds  # command activity streamed


def test_poll_cursor_advances(valid_path, monkeypatch):
    _patch(monkeypatch, _FakePopen(lines=_fixture_lines("command_read.jsonl"), returncode=0))
    mgr = CodexJobManager()
    started = mgr.start("x", path=".")
    final = _wait_done(mgr, started["job_id"])
    # Polling again from the final cursor yields no further events.
    again = mgr.poll(started["job_id"], cursor=final["cursor"])
    assert again["events"] == []


def test_nonzero_exit_becomes_error(valid_path, monkeypatch):
    _patch(monkeypatch, _FakePopen(lines=[], returncode=1, stderr_text="boom"))
    mgr = CodexJobManager()
    started = mgr.start("fail", path=".")
    res = _wait_done(mgr, started["job_id"])
    assert res["status"] == "error"
    assert "boom" in res["error"]


def test_stop_kills_running_job(valid_path, monkeypatch):
    block = threading.Event()
    popen = _FakePopen(block=block)
    _patch(monkeypatch, popen)
    mgr = CodexJobManager()
    started = mgr.start("hang", path=".")
    time.sleep(0.2)
    mgr.stop(started["job_id"])
    assert popen.killed is True
    res = _wait_done(mgr, started["job_id"])
    assert res["status"] == "error"


def test_start_validation(valid_path):
    mgr = CodexJobManager()
    assert "error" in mgr.start("", path=".")
    assert "error" in mgr.start("x", path="")


def test_missing_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aios_core.tools.codex_job.resolve_chat_files_path", lambda p: tmp_path / "nope"
    )
    mgr = CodexJobManager()
    assert "does not exist" in mgr.start("x", path="nope")["error"]


def test_poll_unknown_job():
    assert "error" in CodexJobManager().poll("does-not-exist")


def test_codex_missing_binary(valid_path, monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr("aios_core.tools.codex_job.subprocess.Popen", _raise)
    res = CodexJobManager().start("x", path=".")
    assert "error" in res
    assert "not installed" in res["error"] or "not on PATH" in res["error"]
