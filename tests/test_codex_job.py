"""Interactive app-server Codex job tests (mocked, deterministic)."""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from pathlib import Path

import pytest

from aios_core.tools.codex_job import CodexJobManager
from aios_core.tools.codex_run_store import CodexRunStore
from aios_core.app_git import run_git
from aios_core.deploy.worktree_handoff import WorktreeRegistry


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
        hold_turn: bool = False,
        crash_on_initialize: bool = False,
        on_turn=None,
    ) -> None:
        self.stdout = _QueueStream()
        self.stderr = _QueueStream()
        self.stdin = _FakeStdin(self)
        self.asks_question = asks_question
        self.fail_initialize = fail_initialize
        self.mcp_tools_by_turn = mcp_tools_by_turn or []
        self.mcp_results_by_tool = mcp_results_by_tool or {}
        self.hold_turn = hold_turn
        self.crash_on_initialize = crash_on_initialize
        self.turn_count = 0
        self.on_turn = on_turn
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
            if self.crash_on_initialize:
                self.crash()
            elif self.fail_initialize:
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
            if self.on_turn is not None:
                self.on_turn(message["params"]["input"][0]["text"])
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
            if self.hold_turn:
                return
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

    def crash(self, returncode: int = 17) -> None:
        self.returncode = returncode
        self.stdout.close()
        self.stderr.close()
        self._done.set()


def _patch(monkeypatch, process: _FakeAppServer):
    monkeypatch.setattr(
        "aios_core.tools.codex_job.subprocess.Popen", lambda *args, **kwargs: process
    )


def _patch_sequence(monkeypatch, *processes: _FakeAppServer):
    iterator = iter(processes)
    monkeypatch.setattr(
        "aios_core.tools.codex_job.subprocess.Popen",
        lambda *args, **kwargs: next(iterator),
    )


@pytest.fixture
def valid_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aios_core.tools.codex_job.resolve_chat_files_path", lambda path: tmp_path
    )
    return tmp_path


def _wait_for(
    mgr: CodexJobManager, job_id: str, status: str, timeout: float = 5
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


def _deploy_fixture(tmp_path: Path, monkeypatch, *components: str):
    workspace = tmp_path / "workspace"
    apps_root = workspace / "apps"
    app = apps_root / "app_test123"
    app.mkdir(parents=True)
    _write_deploy_manifest(app, *components)
    run_git(app, ["init", "-b", "main"])
    run_git(app, ["config", "user.name", "AIOS Test"])
    run_git(app, ["config", "user.email", "aios@example.test"])
    run_git(app, ["add", "."])
    run_git(app, ["commit", "-m", "baseline"])
    monkeypatch.setattr(
        "aios_core.tools.codex_job.resolve_chat_files_path", lambda path: app
    )
    registry = WorktreeRegistry(workspace / ".aios" / "worktrees", apps_root=apps_root)
    return app, registry


def _complete_change_handoff(app: Path):
    def complete(prompt: str) -> None:
        job_id = re.search(r"^- job_id: (.+)$", prompt, re.MULTILINE).group(1)
        worktree_id = re.search(r"^- worktree_id: (.+)$", prompt, re.MULTILINE).group(1)
        workspace_path = re.search(
            r"^- reserved detached worktree path: (.+)$", prompt, re.MULTILINE
        ).group(1)
        run_git(app, ["config", "user.name", "AIOS Test"])
        run_git(app, ["config", "user.email", "aios@example.test"])
        head = run_git(app, ["rev-parse", "--verify", "HEAD"], check=False)
        if head.returncode != 0:
            run_git(app, ["add", "."])
            run_git(app, ["commit", "-m", "chore(aios): baseline app"])
        base = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
        (app / "source.txt").write_text("green button\n")
        (app / "HISTORY.md").write_text(
            "2026-08-18T22:00:00Z Changed the button to green.\n"
            f"job_id: {job_id}\nrollback_base: {base}\n"
            "Verification: fixture verification passed.\n"
        )
        run_git(app, ["add", "."])
        run_git(app, ["commit", "-m", "feat: green button"])
        change = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
        tree = run_git(app, ["rev-parse", "HEAD^{tree}"]).stdout.strip()
        run_git(app, ["worktree", "add", "--detach", workspace_path, change])
        descriptor = Path(workspace_path) / ".aios" / "CODEX_HANDOFF.json"
        descriptor.parent.mkdir(parents=True, exist_ok=True)
        descriptor.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "job_id": job_id,
                    "app_id": "app_test123",
                    "mode": "change",
                    "worktree_id": worktree_id,
                    "canonical_repository": str(app),
                    "workspace_path": workspace_path,
                    "base_commit": base,
                    "source_commit": change,
                    "source_tree": tree,
                    "selection_reason": "Changed the primary button to green",
                }
            )
            + "\n"
        )

    return complete


def _complete_selected_handoff(app: Path, selected: str):
    def complete(prompt: str) -> None:
        job_id = re.search(r"^- job_id: (.+)$", prompt, re.MULTILINE).group(1)
        worktree_id = re.search(r"^- worktree_id: (.+)$", prompt, re.MULTILINE).group(1)
        workspace_path = re.search(
            r"^- reserved detached worktree path: (.+)$", prompt, re.MULTILINE
        ).group(1)
        base = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
        tree = run_git(app, ["rev-parse", f"{selected}^{{tree}}"]).stdout.strip()
        run_git(app, ["worktree", "add", "--detach", workspace_path, selected])
        descriptor = Path(workspace_path) / ".aios" / "CODEX_HANDOFF.json"
        descriptor.parent.mkdir(parents=True, exist_ok=True)
        descriptor.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "job_id": job_id,
                    "app_id": "app_test123",
                    "mode": "selected_commit",
                    "worktree_id": worktree_id,
                    "canonical_repository": str(app),
                    "workspace_path": workspace_path,
                    "base_commit": base,
                    "source_commit": selected,
                    "source_tree": tree,
                    "selection_reason": "This commit changed the button to green",
                }
            )
            + "\n"
        )

    return complete


def _complete_record_change(app: Path):
    def complete(prompt: str) -> None:
        job_id = re.search(r"^- job_id: (.+)$", prompt, re.MULTILINE).group(1)
        base = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
        (app / "source.txt").write_text("recorded change\n")
        (app / "HISTORY.md").write_text(
            "2026-08-18T22:00:00Z Changed source and verified it.\n"
            f"job_id: {job_id}\nrollback_base: {base}\n"
        )
        run_git(app, ["add", "."])
        run_git(app, ["commit", "-m", "feat: recorded app change"])

    return complete


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


def test_deploy_contract_returns_validated_workspace_handoff(tmp_path, monkeypatch):
    app, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    process = _FakeAppServer(on_turn=_complete_change_handoff(app))
    _patch(monkeypatch, process)
    mgr = CodexJobManager(worktree_registry=registry)

    started = mgr.start("build the backend", path=".", enable_deploy=True)
    assert "deployment_handoff" not in started
    result = _wait_for(mgr, started["job_id"], "done")

    assert result["status"] == "done"
    assert result["workspace_handoff"]["status"] == "handoff_ready"
    assert result["workspace_handoff"]["workspace_path"].startswith(
        str(registry.checkouts_dir)
    )
    turn = next(
        message for message in process.received if message.get("method") == "turn/start"
    )
    prompt = turn["params"]["input"][0]["text"]
    assert "AIOS APP CHANGE AND WORKSPACE HANDOFF CONTRACT v3" in prompt
    assert "Do not create a later metadata-only commit" in prompt
    assert "you do not deploy it" in prompt
    record = mgr.store.get(started["job_id"])
    assert "deployment_handoff_v3" in record["capabilities"]
    assert record["contract_version"] == 3
    assert record["app_state"]["host_checkpoint"]["source_commit"] == result[
        "workspace_handoff"
    ]["source_commit"]
    assert result["workspace_handoff"]["provenance_commit"] is None
    assert "cloud_deploy" not in record["capabilities"]
    assert "-c" not in mgr.get(started["job_id"]).cmd
    assert registry.get_app_lease("app_test123") is None


def test_deploy_contract_bootstraps_app_root_repository(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    apps_root = workspace / "apps"
    app = apps_root / "app_test123"
    app.mkdir(parents=True)
    _write_deploy_manifest(app, "frontend")
    monkeypatch.setattr(
        "aios_core.tools.codex_job.resolve_chat_files_path", lambda path: app
    )
    registry = WorktreeRegistry(workspace / ".aios" / "worktrees", apps_root=apps_root)
    process = _FakeAppServer(on_turn=_complete_change_handoff(app))
    _patch(monkeypatch, process)
    manager = CodexJobManager(worktree_registry=registry)

    started = manager.start("Build the frontend", path=".", enable_deploy=True)
    result = _wait_for(manager, started["job_id"], "done")

    assert result["status"] == "done"
    assert result["workspace_handoff"]["source_commit"]
    assert (app / ".git").is_dir()


def test_historical_deploy_handoff_leaves_canonical_head_unchanged(
    tmp_path, monkeypatch
):
    app, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    (app / "frontend" / "index.html").write_text("<button>Green</button>\n")
    run_git(app, ["add", "."])
    run_git(app, ["commit", "-m", "feat: make button green"])
    green = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
    (app / "frontend" / "index.html").write_text("<button>Yellow</button>\n")
    run_git(app, ["add", "."])
    run_git(app, ["commit", "-m", "feat: make button yellow"])
    current = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
    process = _FakeAppServer(on_turn=_complete_selected_handoff(app, green))
    _patch(monkeypatch, process)
    manager = CodexJobManager(worktree_registry=registry)

    started = manager.start(
        "Redeploy the version where the button was green",
        path=".",
        enable_deploy=True,
    )
    result = _wait_for(manager, started["job_id"], "done")

    assert result["workspace_handoff"]["mode"] == "selected_commit"
    assert result["workspace_handoff"]["source_commit"] == green
    assert run_git(app, ["rev-parse", "HEAD"]).stdout.strip() == current
    assert not run_git(
        app, ["status", "--porcelain=v1", "--untracked-files=all"]
    ).stdout.strip()


def test_non_deploy_app_change_injects_history_and_commit_contract(
    tmp_path, monkeypatch
):
    app, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    process = _FakeAppServer(on_turn=_complete_record_change(app))
    _patch(monkeypatch, process)
    manager = CodexJobManager(worktree_registry=registry)

    started = manager.start("Change the app copy", path=".")
    result = _wait_for(manager, started["job_id"], "done")

    assert result["status"] == "done"
    assert result["workspace_handoff"] is None
    record = manager.store.get(started["job_id"])
    assert "app_change_v3" in record["capabilities"]
    assert record["app_state"]["completion_mode"] == "change"
    assert record["app_state"]["provenance_commit"] is None
    assert record["app_state"]["host_checkpoint"]["schema_version"] == 2
    prompt = next(
        message for message in process.received if message.get("method") == "turn/start"
    )["params"]["input"][0]["text"]
    assert "AIOS APP CHANGE RECORD CONTRACT v3" in prompt
    assert "UTC ISO-8601 timestamped entry" in prompt
    assert "Do not create a later metadata-only commit" in prompt
    assert "host records the machine-readable checkpoint" in prompt
    assert "EXACT AIOS CHECKPOINT SCHEMA" not in prompt
    assert registry.get_app_lease("app_test123") is None


def test_v3_adopts_and_commits_unfinished_app_changes(tmp_path, monkeypatch):
    app, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    base = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
    (app / "source.txt").write_text("unfinished durable work\n")

    def complete(prompt: str) -> None:
        job_id = re.search(r"^- job_id: (.+)$", prompt, re.MULTILINE).group(1)
        assert "already contained unfinished changes" in prompt
        assert "source.txt" in prompt
        (app / "HISTORY.md").write_text(
            "2026-08-18T22:00:00Z Recovered interrupted app work.\n"
            f"job_id: {job_id}\nrollback_base: {base}\n"
            "Verification: fixture verification passed.\n"
        )
        run_git(app, ["add", "."])
        run_git(app, ["commit", "-m", "feat: finish interrupted app work"])

    process = _FakeAppServer(on_turn=complete)
    _patch(monkeypatch, process)
    manager = CodexJobManager(worktree_registry=registry)

    started = manager.start("Finish the app", path=".")
    assert "error" not in started
    result = _wait_for(manager, started["job_id"], "done")

    assert result["status"] == "done"
    record = manager.store.get(started["job_id"])
    assert record["app_state"]["initial_dirty"] is True
    assert record["app_state"]["base_commit"] == base
    assert record["app_state"]["host_checkpoint"]["source_commit"] == run_git(
        app, ["rev-parse", "HEAD"]
    ).stdout.strip()
    assert not run_git(
        app, ["status", "--porcelain=v1", "--untracked-files=all"]
    ).stdout.strip()


def test_v3_followup_completes_linear_source_range_without_metadata_m(
    tmp_path, monkeypatch
):
    app, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    state: dict[str, str] = {}

    def complete(prompt: str) -> None:
        if not state:
            job_id = re.search(r"^- job_id: (.+)$", prompt, re.MULTILINE).group(1)
            base = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
            (app / "source.txt").write_text("changed\n")
            (app / "HISTORY.md").write_text("Changed source.\n")
            run_git(app, ["add", "."])
            run_git(app, ["commit", "-m", "feat: change source"])
            change = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
            state.update(
                base=base,
                change=change,
                job_id=job_id,
            )
            return

        job_id = state["job_id"]
        assert "satisfy contract v3" in prompt
        assert "Do not create metadata commit M" in prompt
        assert "reset the canonical branch to C" not in prompt
        with (app / "HISTORY.md").open("a") as history:
            history.write(
                f"job_id: {job_id}\nrollback_base: {state['base']}\n"
                "Verification: tests passed.\n"
            )
        run_git(app, ["add", "."])
        run_git(app, ["commit", "-m", "docs: record app change"])

    process = _FakeAppServer(on_turn=complete)
    _patch(monkeypatch, process)
    manager = CodexJobManager(worktree_registry=registry)

    started = manager.start("Change the app copy", path=".")
    result = _wait_for(manager, started["job_id"], "done")

    assert result["status"] == "done", result["error"]
    assert process.turn_count == 2
    head = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
    parent = run_git(app, ["rev-parse", "HEAD^"]).stdout.strip()
    assert parent == state["change"]
    assert head != state["change"]
    assert not (app / ".aios" / "checkpoints" / f"{state['job_id']}.json").exists()
    record = manager.store.get(started["job_id"])
    assert record["app_state"]["host_checkpoint"]["source_commit"] == head


def test_non_deploy_read_only_app_task_keeps_head_unchanged(tmp_path, monkeypatch):
    app, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    original = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
    process = _FakeAppServer()
    _patch(monkeypatch, process)
    manager = CodexJobManager(worktree_registry=registry)

    started = manager.start("Explain the frontend", path=".")
    result = _wait_for(manager, started["job_id"], "done")

    assert result["status"] == "done"
    assert run_git(app, ["rev-parse", "HEAD"]).stdout.strip() == original
    assert manager.store.get(started["job_id"])["app_state"]["completion_mode"] == (
        "read_only"
    )


def test_handoff_guard_continues_same_thread_for_missing_descriptor(
    tmp_path, monkeypatch
):
    _, registry = _deploy_fixture(tmp_path, monkeypatch, "server")
    process = _FakeAppServer()
    _patch(monkeypatch, process)
    mgr = CodexJobManager(worktree_registry=registry)

    started = mgr.start("build and deploy", path=".", enable_deploy=True)
    result = _wait_for(mgr, started["job_id"], "error")

    assert result["status"] == "error"
    turns = [
        message for message in process.received if message.get("method") == "turn/start"
    ]
    assert len(turns) == 3
    assert "host rejected completion" in turns[1]["params"]["input"][0]["text"]
    guard = next(
        event for event in result["events"] if event["kind"] == "app_handoff_guard"
    )
    assert "CODEX_HANDOFF.json" in guard["output"]


def test_handoff_guard_fails_closed_when_descriptor_remains_missing(
    tmp_path, monkeypatch
):
    from aios_core.tools import codex_job as module

    _, registry = _deploy_fixture(tmp_path, monkeypatch, "database")
    process = _FakeAppServer()
    _patch(monkeypatch, process)
    monkeypatch.setattr(module, "MAX_APP_HANDOFF_FOLLOWUPS", 0)
    mgr = CodexJobManager(worktree_registry=registry)

    started = mgr.start("build and deploy", path=".", enable_deploy=True)
    result = _wait_for(mgr, started["job_id"], "error")

    assert "CODEX_HANDOFF.json" in result["error"]
    assert "mandatory AIOS app handoff contract" in result["error"]
    worktree_id = next(registry.records_dir.glob("wt_*.json")).stem
    assert registry.get(worktree_id).status == "removed"


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
    assert mgr.store.get(started["job_id"])["recovery_count"] == 0


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


def test_running_job_resumes_persisted_thread_after_restart(valid_path, monkeypatch):
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
        message
        for message in process.received
        if message.get("method") == "thread/resume"
    )
    assert resume["params"]["threadId"] == "thread-persisted"


def test_unexpected_child_exit_recovers_same_job_and_thread(
    valid_path, monkeypatch
):
    from aios_core.tools import codex_job as module

    monkeypatch.setattr(module, "RECOVERY_BACKOFF_BASE_SECONDS", 0)
    first = _FakeAppServer(hold_turn=True)
    replacement = _FakeAppServer()
    _patch_sequence(monkeypatch, first, replacement)
    store = CodexRunStore(":memory:")
    manager = CodexJobManager(store)
    started = manager.start("Finish the implementation", path=".", session_id="chat-1")
    deadline = time.time() + 3
    while time.time() < deadline and not (store.get(started["job_id"]) or {}).get(
        "thread_id"
    ):
        time.sleep(0.01)

    first.crash()
    result = _wait_for(manager, started["job_id"], "done")

    assert result["status"] == "done"
    assert result["recovery_count"] == 1
    resume = next(
        message
        for message in replacement.received
        if message.get("method") == "thread/resume"
    )
    assert resume["params"]["threadId"] == "thread-1"
    assert store.pending_signals() == [(started["job_id"], "done")]
    recovery_events = [
        event
        for event in result["events"]
        if event.get("kind") == "process_recovery"
    ]
    assert recovery_events[0]["phase"] == "scheduled"


def test_child_exit_before_thread_creation_restarts_from_original_task(
    valid_path, monkeypatch
):
    from aios_core.tools import codex_job as module

    monkeypatch.setattr(module, "RECOVERY_BACKOFF_BASE_SECONDS", 0)
    first = _FakeAppServer(crash_on_initialize=True)
    replacement = _FakeAppServer()
    _patch_sequence(monkeypatch, first, replacement)
    store = CodexRunStore(":memory:")
    manager = CodexJobManager(store)

    started = manager.start("Build the API", path=".")
    result = _wait_for(manager, started["job_id"], "done")

    assert result["status"] == "done"
    assert result["recovery_count"] == 1
    assert len(
        [
            message
            for message in replacement.received
            if message.get("method") == "thread/start"
        ]
    ) == 1
    assert not any(
        message.get("method") == "thread/resume"
        for message in replacement.received
    )


def test_child_exit_uses_ready_handoff_without_starting_replacement(
    tmp_path, monkeypatch
):
    from aios_core.tools import codex_job as module

    monkeypatch.setattr(module, "RECOVERY_BACKOFF_BASE_SECONDS", 0)
    app, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    process = _FakeAppServer(hold_turn=True)
    launches = 0

    def popen(*args, **kwargs):
        nonlocal launches
        launches += 1
        if launches > 1:
            raise AssertionError("ready handoff should not start another Codex child")
        return process

    monkeypatch.setattr("aios_core.tools.codex_job.subprocess.Popen", popen)
    store = CodexRunStore(":memory:")
    manager = CodexJobManager(store, worktree_registry=registry)
    started = manager.start("Deploy the app", path=".", enable_deploy=True)
    deadline = time.time() + 3
    while time.time() < deadline and not (store.get(started["job_id"]) or {}).get(
        "thread_id"
    ):
        time.sleep(0.01)
    reservation_path = next(registry.records_dir.glob("wt_*.json"))
    reserved = registry.get(reservation_path.stem)
    source = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
    tree = run_git(app, ["rev-parse", "HEAD^{tree}"]).stdout.strip()
    run_git(app, ["worktree", "add", "--detach", reserved.path, source])
    published = registry.publish_handoff(
        reserved.worktree_id,
        source_commit=source,
        source_tree=tree,
        selection_reason="Source was validated before the child exited",
    )

    process.crash()
    result = _wait_for(manager, started["job_id"], "done")

    assert result["workspace_handoff"]["handoff_id"] == published.handoff_id
    assert launches == 1
    assert registry.get_app_lease("app_test123") is None


def test_replacement_launch_failures_exhaust_recovery_and_notify_once(
    tmp_path, monkeypatch
):
    from aios_core.tools import codex_job as module

    monkeypatch.setattr(module, "RECOVERY_BACKOFF_BASE_SECONDS", 0)
    monkeypatch.setattr(module, "MAX_RECOVERY_ATTEMPTS", 2)
    app, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    first = _FakeAppServer(hold_turn=True)
    launches = 0

    def popen(*args, **kwargs):
        nonlocal launches
        launches += 1
        if launches == 1:
            return first
        raise FileNotFoundError("replacement binary unavailable")

    monkeypatch.setattr("aios_core.tools.codex_job.subprocess.Popen", popen)
    store = CodexRunStore(":memory:")
    manager = CodexJobManager(store, worktree_registry=registry)
    started = manager.start(
        "Deploy the app", path=str(app), enable_deploy=True, session_id="chat-1"
    )
    deadline = time.time() + 3
    while time.time() < deadline and not (store.get(started["job_id"]) or {}).get(
        "thread_id"
    ):
        time.sleep(0.01)

    first.crash()
    result = _wait_for(manager, started["job_id"], "error")

    assert "recovery was exhausted after 2 attempts" in result["error"]
    assert result["recovery_count"] == 2
    assert launches == 3
    assert store.pending_signals() == [(started["job_id"], "error")]
    assert registry.get_app_lease("app_test123") is None
    worktree = registry.get(next(registry.records_dir.glob("wt_*.json")).stem)
    assert worktree.status == "removed"


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
    assert (
        len(
            [
                message
                for message in process.received
                if message.get("method") == "turn/start"
            ]
        )
        == 1
    )


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


def test_durable_app_lease_rejects_a_second_manager(tmp_path, monkeypatch):
    app, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    process = _FakeAppServer(asks_question=True)
    _patch(monkeypatch, process)
    first_manager = CodexJobManager(worktree_registry=registry)
    second_manager = CodexJobManager(worktree_registry=registry)

    first = first_manager.start("Change the app", path=".")
    _wait_for(first_manager, first["job_id"], "awaiting_input")
    second = second_manager.start("Make another change", path=".")

    assert "Another Codex job is already changing this app" in second["error"]
    assert registry.get_app_lease("app_test123").owner_job_id == first["job_id"]
    assert first_manager.stop(first["job_id"])["status"] == "cancelled"
    assert registry.get_app_lease("app_test123") is None


def test_failed_codex_start_releases_app_lease_and_reservation(
    tmp_path, monkeypatch
):
    app, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    monkeypatch.setattr(
        "aios_core.tools.codex_job.subprocess.Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    manager = CodexJobManager(worktree_registry=registry)

    result = manager.start("Deploy the app", path=str(app), enable_deploy=True)

    assert "not installed" in result["error"]
    assert registry.get_app_lease("app_test123") is None
    records = list(registry.records_dir.glob("wt_*.json"))
    assert len(records) == 1
    assert registry.get(records[0].stem).status == "removed"


def test_restart_reacquires_missing_lease_for_awaiting_app_job(
    tmp_path, monkeypatch
):
    app, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    store = CodexRunStore(":memory:")
    store.create(
        job_id="recover-app-input",
        session_id="chat-1",
        parent_run_id="run-1",
        parent_tool_call_id="tool-1",
        task="Change the app",
        workdir=str(app),
        model=None,
        capabilities=["filesystem", "shell", "app_change_v3"],
        contract_version=3,
        app_state={
            "app_id": "app_test123",
            "app_root": str(app),
            "deployment_requested": False,
        },
    )
    store.update(
        "recover-app-input",
        status="awaiting_input",
        thread_id="thread-1",
        pending_input={"questions": [{"id": "choice"}]},
    )
    manager = CodexJobManager(store, worktree_registry=registry)

    assert manager.reconcile_stale() == []
    assert registry.get_app_lease("app_test123").owner_job_id == (
        "recover-app-input"
    )
    assert manager.stop("recover-app-input")["status"] == "cancelled"
    assert registry.get_app_lease("app_test123") is None


def test_restart_finalizes_an_already_published_handoff_without_codex(
    tmp_path, monkeypatch
):
    app, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    store = CodexRunStore(":memory:")
    job_id = "recover-ready"
    registry.acquire_app_lease(
        app_id="app_test123", repository=app, owner_job_id=job_id
    )
    reserved = registry.reserve(
        app_id="app_test123",
        repository=app,
        owner_job_id=job_id,
        purpose="prepare_deployment_source",
    )
    source = run_git(app, ["rev-parse", "HEAD"]).stdout.strip()
    tree = run_git(app, ["rev-parse", "HEAD^{tree}"]).stdout.strip()
    run_git(app, ["worktree", "add", "--detach", reserved.path, source])
    published = registry.publish_handoff(
        reserved.worktree_id,
        source_commit=source,
        source_tree=tree,
        selection_reason="Validated before the host restart",
    )
    store.create(
        job_id=job_id,
        session_id="chat-1",
        parent_run_id="run-1",
        parent_tool_call_id="tool-1",
        task="Deploy the existing app",
        workdir=str(app),
        model=None,
        capabilities=["filesystem", "shell", "deployment_handoff_v3"],
        contract_version=3,
        deployment_requested=True,
        app_state={
            "contract_version": 3,
            "deployment_requested": True,
            "app_id": "app_test123",
            "app_root": str(app),
            "worktree_id": reserved.worktree_id,
            "workspace_path": reserved.path,
        },
    )

    manager = CodexJobManager(store, worktree_registry=registry)
    assert manager.reconcile_stale() == [job_id]

    record = store.get(job_id)
    assert record is not None
    assert record["status"] == "done"
    assert record["workspace_handoff"]["handoff_id"] == published.handoff_id
    assert record["workspace_handoff"]["status"] == "handoff_ready"
    assert store.pending_signals() == [(job_id, "done")]
    assert registry.get_app_lease("app_test123") is None


def test_graceful_restart_preserves_active_app_job_and_lease(
    tmp_path, monkeypatch
):
    _, registry = _deploy_fixture(tmp_path, monkeypatch, "frontend")
    process = _FakeAppServer(asks_question=True)
    _patch(monkeypatch, process)
    store = CodexRunStore(":memory:")
    manager = CodexJobManager(store, worktree_registry=registry)
    started = manager.start("Change the app", path=".")
    _wait_for(manager, started["job_id"], "awaiting_input")

    assert manager.interrupt_all_for_restart() == [started["job_id"]]

    record = store.get(started["job_id"])
    assert record is not None
    assert record["status"] == "awaiting_input"
    assert record["finished_at"] is None
    assert store.pending_signals() == [(started["job_id"], "awaiting_input")]
    assert registry.get_app_lease("app_test123").owner_job_id == started["job_id"]
    assert process.killed is True


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
