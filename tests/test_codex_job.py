"""Interactive app-server Codex job tests (mocked, deterministic)."""

from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

import pytest

from aios_core.tools.codex_job import CodexJobManager
from aios_core.tools.codex_run_store import CodexRunStore


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
        self,
        *,
        asks_question: bool = False,
        fail_initialize: bool = False,
        mcp_tools_by_turn: list[list[str]] | None = None,
        mcp_results_by_tool: dict[str, dict] | None = None,
    ) -> None:
        self.stdout = _QueueStream()
        self.stderr = _QueueStream()
        self.stdin = _FakeStdin(self)
        self.asks_question = asks_question
        self.fail_initialize = fail_initialize
        self.mcp_tools_by_turn = mcp_tools_by_turn or []
        self.mcp_results_by_tool = mcp_results_by_tool or {}
        self.turn_count = 0
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
        elif method == "thread/resume":
            self.stdout.push(
                {
                    "id": request_id,
                    "result": {"thread": {"id": message["params"]["threadId"]}},
                }
            )
        elif method == "turn/start":
            self.turn_count += 1
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
            tools = (
                self.mcp_tools_by_turn[self.turn_count - 1]
                if self.turn_count <= len(self.mcp_tools_by_turn)
                else []
            )
            for index, tool in enumerate(tools):
                tool_result = self.mcp_results_by_tool.get(
                    tool, {"id": f"dep_{tool}", "status": "queued"}
                )
                item = {
                    "id": f"mcp-{self.turn_count}-{index}",
                    "type": "mcpToolCall",
                    "server": "deploy",
                    "tool": tool,
                    "arguments": {},
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(tool_result),
                            }
                        ]
                    },
                    "status": "completed",
                }
                self.stdout.push(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "item": {**item, "status": "inProgress"},
                        },
                    }
                )
                self.stdout.push(
                    {
                        "method": "item/completed",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "item": item,
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


def _write_deploy_manifest(root: Path, *components: str) -> None:
    lines = ["version: 1", "app_id: app_test123"]
    for component in components:
        lines.append(f"{component}: {{}}")
        if component == "database":
            (root / "database" / "migrations").mkdir(parents=True)
        elif component == "server":
            (root / "server").mkdir()
            (root / "server" / "Dockerfile").write_text("FROM scratch\n")
        elif component == "frontend":
            (root / "frontend").mkdir()
            (root / "frontend" / "index.html").write_text("ok\n")
    (root / "aios.deploy.yaml").write_text("\n".join(lines) + "\n")


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
    record = mgr.store.get(started["job_id"])
    assert record is not None
    assert record["status"] == "done"
    assert record["result"] == "Implemented it."
    assert "cloud_deploy" not in record["capabilities"]
    assert "-c" not in mgr.get(started["job_id"]).cmd
    assert "error" in mgr.poll(started["job_id"], session_id="another-chat")
    assert process.stdin is not None
    assert any(message.get("method") == "initialize" for message in process.received)
    assert any(message.get("method") == "thread/start" for message in process.received)
    assert any(message.get("method") == "turn/start" for message in process.received)


def test_deploy_contract_calls_matching_tools_and_completes(valid_path, monkeypatch):
    _write_deploy_manifest(valid_path, "database", "server")
    process = _FakeAppServer(
        mcp_tools_by_turn=[["deploy_database", "deploy_server"]]
    )
    _patch(monkeypatch, process)
    mgr = CodexJobManager()

    started = mgr.start("build the backend", path=".", enable_deploy=True)
    result = _wait_for(mgr, started["job_id"], "done")

    assert result["status"] == "done"
    turn = next(
        message for message in process.received if message.get("method") == "turn/start"
    )
    prompt = turn["params"]["input"][0]["text"]
    assert "AIOS CLOUD DEPLOYMENT CONTRACT (MANDATORY)" in prompt
    assert "deploy_database" in prompt
    assert "deploy_server" in prompt
    assert "wait until the database is active" in prompt
    assert not any(event["kind"] == "deployment_guard" for event in result["events"])


def test_deploy_guard_continues_same_thread_for_missing_call(
    valid_path, monkeypatch
):
    _write_deploy_manifest(valid_path, "server")
    process = _FakeAppServer(mcp_tools_by_turn=[[], ["deploy_server"]])
    _patch(monkeypatch, process)
    mgr = CodexJobManager()

    started = mgr.start("build and deploy", path=".", enable_deploy=True)
    result = _wait_for(mgr, started["job_id"], "done")

    assert result["status"] == "done"
    turns = [
        message for message in process.received if message.get("method") == "turn/start"
    ]
    assert len(turns) == 2
    assert "host rejected completion" in turns[1]["params"]["input"][0]["text"]
    guard = next(event for event in result["events"] if event["kind"] == "deployment_guard")
    assert "deploy_server" in guard["output"]


def test_deploy_guard_fails_closed_when_calls_remain_missing(
    valid_path, monkeypatch
):
    from aios_core.tools import codex_job as module

    _write_deploy_manifest(valid_path, "database")
    process = _FakeAppServer()
    _patch(monkeypatch, process)
    monkeypatch.setattr(module, "MAX_DEPLOY_FOLLOWUPS", 0)
    mgr = CodexJobManager()

    started = mgr.start("build and deploy", path=".", enable_deploy=True)
    result = _wait_for(mgr, started["job_id"], "error")

    assert "deploy_database" in result["error"]
    assert "mandatory AIOS deployment contract" in result["error"]


def test_deploy_guard_requires_deployment_id_and_preserves_exact_error(
    valid_path, monkeypatch
):
    from aios_core.tools import codex_job as module

    _write_deploy_manifest(valid_path, "frontend")
    process = _FakeAppServer(
        mcp_tools_by_turn=[["deploy_frontend"]],
        mcp_results_by_tool={
            "deploy_frontend": {
                "status": "error",
                "component": "frontend",
                "error": "Artifact is missing frontend source",
            }
        },
    )
    _patch(monkeypatch, process)
    monkeypatch.setattr(module, "MAX_DEPLOY_FOLLOWUPS", 0)
    mgr = CodexJobManager()

    started = mgr.start("build and deploy", path=".", enable_deploy=True)
    result = _wait_for(mgr, started["job_id"], "error")

    assert "did not return a deployment ID: deploy_frontend" in result["error"]
    assert "Artifact is missing frontend source" in result["error"]


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
    lifecycle: list[tuple[str, str, str]] = []
    module.set_progress_sink(
        lambda session_id, kind, payload: captured.append((session_id, kind, payload))
    )
    module.set_lifecycle_sink(
        lambda session_id, job_id, status: lifecycle.append(
            (session_id, job_id, status)
        )
    )
    try:
        mgr = CodexJobManager()
        started = mgr.start("build", path=".", session_id="chat-1")
        _wait_for(mgr, started["job_id"], "awaiting_input")
        mgr.answer(started["job_id"], {"framework": "FastAPI"})
        _wait_for(mgr, started["job_id"], "done")
    finally:
        module.set_lifecycle_sink(None)
        module.set_progress_sink(None)
    kinds = [kind for _, kind, _ in captured]
    assert "codex.started" in kinds
    assert "codex.input.requested" in kinds
    assert "codex.input.resolved" in kinds
    assert "codex.completed" in kinds
    assert all(session_id == "chat-1" for session_id, _, _ in captured)
    assert [status for _, _, status in lifecycle] == ["awaiting_input", "done"]


def test_validation_and_unknown_job(valid_path):
    mgr = CodexJobManager()
    assert "error" in mgr.start("", path=".")
    assert "error" in mgr.start("x", path="")
    assert "error" in mgr.poll("missing")
    assert "error" in mgr.answer("missing", {"x": "y"})


def test_running_job_resumes_persisted_thread_after_restart(
    valid_path, monkeypatch
):
    process = _FakeAppServer()
    _patch(monkeypatch, process)
    store = CodexRunStore(":memory:")
    store.create(
        job_id="recover-1",
        session_id="chat-1",
        parent_run_id="parent-1",
        parent_tool_call_id="tool-1",
        task="finish the implementation",
        workdir=str(valid_path),
        model=None,
        capabilities=["filesystem", "shell"],
    )
    store.update("recover-1", thread_id="thread-persisted")
    mgr = CodexJobManager(store)

    assert mgr.reconcile_stale() == ["recover-1"]
    result = _wait_for(mgr, "recover-1", "done")

    assert result["result"] == "Implemented it."
    assert result["recovery_count"] == 1
    resume = next(
        message for message in process.received if message.get("method") == "thread/resume"
    )
    assert resume["params"]["threadId"] == "thread-persisted"


def test_recovery_preserves_completed_deployments_without_reenqueuing(
    valid_path, monkeypatch
):
    _write_deploy_manifest(valid_path, "server")
    process = _FakeAppServer()
    _patch(monkeypatch, process)
    store = CodexRunStore(":memory:")
    store.create(
        job_id="recover-deploy",
        session_id="chat-1",
        parent_run_id="parent-1",
        parent_tool_call_id="tool-1",
        task="build and deploy the server",
        workdir=str(valid_path),
        model=None,
        capabilities=["filesystem", "shell", "cloud_deploy"],
    )
    store.update(
        "recover-deploy",
        thread_id="thread-persisted",
        deploy_state={
            "called": ["deploy_server"],
            "enqueued": ["deploy_server"],
            "followups": 0,
        },
    )
    mgr = CodexJobManager(store)

    assert mgr.reconcile_stale() == ["recover-deploy"]
    result = _wait_for(mgr, "recover-deploy", "done")

    assert result["status"] == "done"
    assert not any(event["kind"] == "deployment_guard" for event in result["events"])
    assert len(
        [
            message
            for message in process.received
            if message.get("method") == "turn/start"
        ]
    ) == 1


def test_offline_awaiting_input_resumes_after_answer(valid_path, monkeypatch):
    process = _FakeAppServer()
    _patch(monkeypatch, process)
    store = CodexRunStore(":memory:")
    store.create(
        job_id="recover-input",
        session_id="chat-1",
        parent_run_id="parent-1",
        parent_tool_call_id="tool-1",
        task="build the API",
        workdir=str(valid_path),
        model=None,
        capabilities=["filesystem", "shell"],
    )
    store.update(
        "recover-input",
        status="awaiting_input",
        thread_id="thread-persisted",
        pending_input={"questions": [{"id": "framework", "question": "Which?"}]},
    )
    mgr = CodexJobManager(store)

    mgr.reconcile_stale()
    answered = mgr.answer(
        "recover-input", {"framework": "FastAPI"}, session_id="chat-1"
    )
    assert answered["recovered"] is True
    result = _wait_for(mgr, "recover-input", "done")
    assert result["status"] == "done"
    turn_start = next(
        message for message in process.received if message.get("method") == "turn/start"
    )
    assert "FastAPI" in turn_start["params"]["input"][0]["text"]


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
