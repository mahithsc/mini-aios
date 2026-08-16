"""Interactive app-server Codex job tests (mocked, deterministic)."""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from aios_core.tools.codex_job import CodexJobManager


class _QueueStream:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()

    def push(self, message: dict) -> None:
        self.lines.put(json.dumps(message) + "\n")

    def close(self) -> None:
        self.lines.put(None)

    def readline(self) -> str:
        line = self.lines.get()
        return "" if line is None else line


class _FakeStdin:
    def __init__(self, process: _FakeAppServer) -> None:
        self.process = process

    def write(self, value: str) -> int:
        for line in value.splitlines():
            if line.strip():
                self.process.receive(json.loads(line))
        return len(value)

    def flush(self) -> None:
        pass


class _FakeAppServer:
    def __init__(
        self, *, asks_question: bool = False, fail_initialize: bool = False
    ) -> None:
        self.stdout = _QueueStream()
        self.stderr = _QueueStream()
        self.stdin = _FakeStdin(self)
        self.asks_question = asks_question
        self.fail_initialize = fail_initialize
        self.returncode: int | None = None
        self.killed = False
        self.received: list[dict] = []
        self.answer: dict | None = None
        self._done = threading.Event()

    def receive(self, message: dict) -> None:
        self.received.append(message)
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            if self.fail_initialize:
                self.stdout.push(
                    {"id": request_id, "error": {"code": -1, "message": "boom"}}
                )
            else:
                self.stdout.push({"id": request_id, "result": {"userAgent": "fake"}})
        elif method == "thread/start":
            self.stdout.push(
                {
                    "id": request_id,
                    "result": {"thread": {"id": "thread-1"}, "model": "fake"},
                }
            )
        elif method == "turn/start":
            self.stdout.push({"id": request_id, "result": {"turn": {"id": "turn-1"}}})
            self.stdout.push(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "id": "cmd-1",
                            "type": "commandExecution",
                            "command": "rg --files",
                            "status": "inProgress",
                        },
                    },
                }
            )
            self.stdout.push(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {
                            "id": "cmd-1",
                            "type": "commandExecution",
                            "command": "rg --files",
                            "aggregatedOutput": "a.py\n",
                            "exitCode": 0,
                            "status": "completed",
                        },
                    },
                }
            )
            if self.asks_question:
                self.stdout.push(
                    {
                        "id": "question-1",
                        "method": "item/tool/requestUserInput",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "itemId": "ask-item-1",
                            "isBlocking": True,
                            "questions": [
                                {
                                    "id": "framework",
                                    "header": "Framework",
                                    "question": "Which framework?",
                                    "isOther": True,
                                    "isSecret": False,
                                    "options": [
                                        {
                                            "label": "FastAPI",
                                            "description": "Python API",
                                        },
                                        {"label": "Flask", "description": "Small app"},
                                    ],
                                }
                            ],
                        },
                    }
                )
            else:
                self._complete_turn()
        elif request_id == "question-1" and "result" in message:
            self.answer = message["result"]
            self._complete_turn()
        elif method == "turn/interrupt":
            self.stdout.push({"id": request_id, "result": {}})

    def _complete_turn(self) -> None:
        message = {"id": "msg-1", "type": "agentMessage", "text": "Implemented it."}
        self.stdout.push(
            {
                "method": "item/completed",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "item": message},
            }
        )
        self.stdout.push(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {
                        "id": "turn-1",
                        "status": "completed",
                        "items": [message],
                        "error": None,
                    },
                },
            }
        )

    def poll(self):
        return self.returncode

    def wait(self):
        self._done.wait(timeout=2)
        return self.returncode if self.returncode is not None else 0

    def terminate(self):
        self.killed = True
        self.returncode = 0
        self.stdout.close()
        self.stderr.close()
        self._done.set()

    def kill(self):
        self.terminate()


def _patch(monkeypatch, process: _FakeAppServer):
    monkeypatch.setattr(
        "aios_core.tools.codex_job.subprocess.Popen", lambda *args, **kwargs: process
    )


@pytest.fixture
def valid_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aios_core.tools.codex_job.resolve_chat_files_path", lambda path: tmp_path
    )
    return tmp_path


def _wait_for(
    mgr: CodexJobManager, job_id: str, status: str, timeout: float = 3
) -> dict:
    deadline = time.time() + timeout
    result = mgr.poll(job_id)
    while time.time() < deadline and result.get("status") != status:
        time.sleep(0.01)
        result = mgr.poll(job_id)
    return result


def test_app_server_happy_path_streams_and_completes(valid_path, monkeypatch):
    process = _FakeAppServer()
    _patch(monkeypatch, process)
    mgr = CodexJobManager()

    started = mgr.start("implement it", path=".")
    result = _wait_for(mgr, started["job_id"], "done")

    assert result["status"] == "done"
    assert result["thread_id"] == "thread-1"
    assert result["turn_id"] == "turn-1"
    assert result["result"] == "Implemented it."
    assert [event["kind"] for event in result["events"]] == ["tool_start", "tool_end"]
    assert process.stdin is not None
    assert any(message.get("method") == "initialize" for message in process.received)
    assert any(message.get("method") == "thread/start" for message in process.received)
    assert any(message.get("method") == "turn/start" for message in process.received)


def test_question_pauses_answer_resumes_same_turn(valid_path, monkeypatch):
    process = _FakeAppServer(asks_question=True)
    _patch(monkeypatch, process)
    mgr = CodexJobManager()
    started = mgr.start("build it", path=".", session_id="chat-1")

    paused = _wait_for(mgr, started["job_id"], "awaiting_input")
    assert paused["error"] is None
    assert paused["pending_input"]["questions"][0]["id"] == "framework"
    assert paused["events"][-1]["kind"] == "input_requested"

    answered = mgr.answer(started["job_id"], {"framework": "FastAPI"})
    assert answered["status"] in {"running", "done"}
    result = _wait_for(mgr, started["job_id"], "done")
    assert result["result"] == "Implemented it."
    assert process.answer == {"answers": {"framework": {"answers": ["FastAPI"]}}}


def test_answer_requires_all_questions(valid_path, monkeypatch):
    process = _FakeAppServer(asks_question=True)
    _patch(monkeypatch, process)
    mgr = CodexJobManager()
    started = mgr.start("build it", path=".")
    _wait_for(mgr, started["job_id"], "awaiting_input")
    assert "missing answers" in mgr.answer(started["job_id"], {"other": "x"})["error"]


def test_initialize_error_is_preserved(valid_path, monkeypatch):
    process = _FakeAppServer(fail_initialize=True)
    _patch(monkeypatch, process)
    mgr = CodexJobManager()
    started = mgr.start("fail", path=".")
    result = _wait_for(mgr, started["job_id"], "error")
    assert "boom" in result["error"]


def test_stop_interrupts_and_cancels(valid_path, monkeypatch):
    process = _FakeAppServer(asks_question=True)
    _patch(monkeypatch, process)
    mgr = CodexJobManager()
    started = mgr.start("wait", path=".")
    _wait_for(mgr, started["job_id"], "awaiting_input")
    result = mgr.stop(started["job_id"])
    assert result["status"] == "cancelled"
    assert process.killed is True


def test_progress_sink_emits_input_and_completion(valid_path, monkeypatch):
    from aios_core.tools import codex_job as module

    process = _FakeAppServer(asks_question=True)
    _patch(monkeypatch, process)
    captured: list[tuple[str, str, dict]] = []
    module.set_progress_sink(
        lambda session_id, kind, payload: captured.append((session_id, kind, payload))
    )
    try:
        mgr = CodexJobManager()
        started = mgr.start("build", path=".", session_id="chat-1")
        _wait_for(mgr, started["job_id"], "awaiting_input")
        mgr.answer(started["job_id"], {"framework": "FastAPI"})
        _wait_for(mgr, started["job_id"], "done")
    finally:
        module.set_progress_sink(None)
    kinds = [kind for _, kind, _ in captured]
    assert "codex.started" in kinds
    assert "codex.input.requested" in kinds
    assert "codex.input.resolved" in kinds
    assert "codex.completed" in kinds
    assert all(session_id == "chat-1" for session_id, _, _ in captured)


def test_validation_and_unknown_job(valid_path):
    mgr = CodexJobManager()
    assert "error" in mgr.start("", path=".")
    assert "error" in mgr.start("x", path="")
    assert "error" in mgr.poll("missing")
    assert "error" in mgr.answer("missing", {"x": "y"})


def test_rejects_concurrent_jobs_in_same_workdir(valid_path, monkeypatch):
    process = _FakeAppServer(asks_question=True)
    _patch(monkeypatch, process)
    mgr = CodexJobManager()
    first = mgr.start("first", path=".")
    _wait_for(mgr, first["job_id"], "awaiting_input")
    second = mgr.start("second", path=".")
    assert "already editing" in second["error"]


def test_missing_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aios_core.tools.codex_job.resolve_chat_files_path",
        lambda path: tmp_path / "missing",
    )
    assert "does not exist" in CodexJobManager().start("x", path=".")["error"]


def test_missing_binary(valid_path, monkeypatch):
    monkeypatch.setattr(
        "aios_core.tools.codex_job.subprocess.Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    assert "not installed" in CodexJobManager().start("x", path=".")["error"]
