from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from aios_core import runtime_context
from aios_core.db import initialize_app_db
from aios_core.tools.codex_run_store import CodexRunStore
from aios_core.tools.codex_job import CodexJobManager
from server.execution.runners import chat as chat_runner_module
from server.execution.service import RunsService
from server.execution.service import build_run_event
from server.execution.store import FileRunStore
from server.gateway.store import insert_gateway_event, list_gateway_events_after
from server.server import _submit_codex_continuation
from server.types.run import Run, RunCreateRequest


def _create_record(
    store: CodexRunStore,
    *,
    job_id: str = "job-1",
    session_id: str = "chat-1",
    parent_run_id: str = "run-1",
    workdir: str = "/tmp/project",
) -> None:
    store.create(
        job_id=job_id,
        session_id=session_id,
        parent_run_id=parent_run_id,
        parent_tool_call_id="tool-1",
        task="Implement the requested change",
        workdir=workdir,
        model=None,
        capabilities=["filesystem", "shell"],
    )


def test_codex_run_store_survives_reopen(tmp_path: Path) -> None:
    db_path = str(tmp_path / "state.db")
    first = CodexRunStore(db_path)
    _create_record(first)
    first.append_event("job-1", {"kind": "tool_start", "tool_name": "shell"})
    first.update("job-1", status="done", result="Implemented it", terminal=True)
    first.enqueue_signal("job-1", "done")

    reopened = CodexRunStore(db_path)
    record = reopened.get("job-1")
    assert record is not None
    assert record["parent_run_id"] == "run-1"
    assert record["status"] == "done"
    assert record["result"] == "Implemented it"
    events, cursor = reopened.events_after("job-1")
    assert events == [{"kind": "tool_start", "tool_name": "shell"}]
    assert cursor == 1
    assert reopened.pending_signals() == [("job-1", "done")]


def test_signal_claim_is_exactly_once() -> None:
    store = CodexRunStore(":memory:")
    _create_record(store)
    store.enqueue_signal("job-1", "done")

    assert store.claim_signal("job-1", "done") is True
    assert store.claim_signal("job-1", "done") is False
    store.complete_signal("job-1", "done")
    assert store.pending_signals() == []


def test_gateway_event_outbox_replays_until_acknowledged() -> None:
    store = CodexRunStore(":memory:")
    _create_record(store)
    sequence = store.append_gateway_event(
        "job-1", "chat-1", "codex.progress", {"kind": "command"}
    )

    assert store.pending_gateway_events() == [
        {
            "job_id": "job-1",
            "sequence": sequence,
            "session_id": "chat-1",
            "event_type": "codex.progress",
            "payload": {
                "kind": "command",
                "codex_event_id": f"job-1:{sequence}",
            },
        }
    ]
    events, _ = store.events_after("job-1")
    assert events == []
    store.complete_gateway_event("job-1", sequence)
    assert store.pending_gateway_events() == []


def test_gateway_replay_is_idempotent_by_codex_event_id(tmp_path: Path) -> None:
    db_path = str(tmp_path / "gateway.db")
    initialize_app_db(db_path)
    payload = {"job_id": "job-1", "codex_event_id": "job-1:4"}

    first = insert_gateway_event(
        "chat-1", "codex.progress", payload, db_path=db_path
    )
    second = insert_gateway_event(
        "chat-1", "codex.progress", payload, db_path=db_path
    )

    assert first["id"] == second["id"]
    assert len(list_gateway_events_after("chat-1", db_path=db_path)) == 1


def test_restart_reconciliation_without_thread_reports_recovery_error() -> None:
    store = CodexRunStore(":memory:")
    _create_record(store)
    manager = CodexJobManager(store)

    assert manager.reconcile_stale() == []
    record = store.get("job-1")
    assert record is not None
    assert record["status"] == "error"
    assert "thread id" in record["error"]
    assert store.pending_signals() == [("job-1", "error")]


def test_completion_signal_queues_one_main_agent_continuation() -> None:
    store = CodexRunStore(":memory:")
    _create_record(store)
    store.update("job-1", status="done", result="ok", terminal=True)
    store.enqueue_signal("job-1", "done")

    class _Manager:
        def __init__(self) -> None:
            self.store = store

        def emit_status(self, *args, **kwargs) -> None:
            pass

    class _Runs:
        def __init__(self) -> None:
            self.requests = []

        async def submit_run(self, request):
            self.requests.append(request)
            return SimpleNamespace(id="continuation-1")

    runs = _Runs()

    async def exercise() -> None:
        first = await _submit_codex_continuation(
            _Manager(), runs, "chat-1", "job-1", "done"
        )
        second = await _submit_codex_continuation(
            _Manager(), runs, "chat-1", "job-1", "done"
        )
        assert first is True
        assert second is False

    asyncio.run(exercise())
    assert len(runs.requests) == 1
    assert runs.requests[0].sourceId == "codex:job-1"
    assert runs.requests[0].turnId == "done"
    signal = store.signal("job-1", "done")
    assert signal is not None
    assert signal["continuation_run_id"] == "continuation-1"


def test_continuation_run_creation_is_idempotent_across_redelivery(
    tmp_path: Path,
) -> None:
    class _Broadcaster:
        async def broadcast_run_accepted(self, run) -> None:
            pass

    service = RunsService(
        FileRunStore(
            metadata_dir=tmp_path / "metadata",
            snapshots_dir=tmp_path / "snapshots",
            events_dir=tmp_path / "events",
        ),
        _Broadcaster(),  # type: ignore[arg-type]
    )
    request = RunCreateRequest(
        kind="chat",
        chatId="chat-1",
        sourceId="codex:job-1",
        turnId="done",
    )

    async def exercise() -> tuple[Run, Run]:
        return await service.submit_run(request), await service.submit_run(request)

    first, second = asyncio.run(exercise())
    assert first.id == second.id
    assert service._queue.qsize() == 1


def test_accepted_codex_continuation_is_requeued_after_restart(
    tmp_path: Path,
) -> None:
    class _Broadcaster:
        async def broadcast_run_accepted(self, run) -> None:
            pass

        async def broadcast_run_event(self, event) -> None:
            pass

    store = FileRunStore(
        metadata_dir=tmp_path / "metadata",
        snapshots_dir=tmp_path / "snapshots",
        events_dir=tmp_path / "events",
    )
    request = RunCreateRequest(
        kind="chat",
        chatId="chat-1",
        sourceId="codex:job-1",
        turnId="done",
    )

    async def exercise() -> tuple[RunsService, Run]:
        first_service = RunsService(store, _Broadcaster())  # type: ignore[arg-type]
        run = await first_service.submit_run(request)
        store.save_snapshot(run.id, status="running", last_sequence=0)

        restarted = RunsService(store, _Broadcaster())  # type: ignore[arg-type]
        await restarted._reconcile_stale_runs()
        return restarted, run

    restarted, run = asyncio.run(exercise())
    assert restarted.get_run(run.id).status == "queued"  # type: ignore[union-attr]
    assert restarted._queue.qsize() == 1


def test_display_status_exposes_verification_phase() -> None:
    store = CodexRunStore(":memory:")
    _create_record(store)
    store.update(
        "job-1",
        status="done",
        terminal=True,
        verification_status="running",
    )
    record = store.get("job-1")
    assert record is not None
    assert record["display_status"] == "verifying_changes"


def test_metrics_and_retention_cleanup_terminal_records(tmp_path: Path) -> None:
    db_path = str(tmp_path / "codex.db")
    store = CodexRunStore(db_path)
    _create_record(store)
    store.update("job-1", status="done", terminal=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE codex_runs SET finished_at = 1 WHERE id = 'job-1'")

    assert store.metrics()["status_counts"] == {"done": 1}
    assert store.cleanup(retention_days=1) == 1
    assert store.get("job-1") is None


def test_terminal_child_flows_through_verified_main_continuation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from aios_core.tools import codex_job as codex_job_module

    child_store = CodexRunStore(":memory:")
    _create_record(child_store)
    child_store.update(
        "job-1",
        status="done",
        result="Changed app.py",
        app_state={
            "workspace_handoff": {
                "handoff_id": "wh_ready123",
                "status": "handoff_ready",
            }
        },
        terminal=True,
    )
    child_store.enqueue_signal("job-1", "done")
    manager = CodexJobManager(child_store)

    class _Broadcaster:
        async def broadcast_run_accepted(self, run) -> None:
            pass

        async def broadcast_run_event(self, event) -> None:
            pass

    run_store = FileRunStore(
        metadata_dir=tmp_path / "metadata",
        snapshots_dir=tmp_path / "snapshots",
        events_dir=tmp_path / "events",
    )
    service = RunsService(run_store, _Broadcaster())  # type: ignore[arg-type]

    async def submit() -> Run:
        accepted = await _submit_codex_continuation(
            manager, service, "chat-1", "job-1", "done"
        )
        assert accepted is True
        signal = child_store.signal("job-1", "done")
        assert signal is not None
        continuation = service.get_run(str(signal["continuation_run_id"]))
        assert continuation is not None
        return continuation

    continuation = asyncio.run(submit())
    monkeypatch.setattr(chat_runner_module, "codex_job_manager", manager)
    messages = chat_runner_module._codex_context_messages(continuation)
    assert "Changed app.py" in str(messages[0].content)
    assert "wh_ready123" in str(messages[0].content)
    assert "create_app_artifact with only its exact handoff_id" in str(
        messages[0].content
    )
    assert "run proportionate verification" in str(messages[0].content)

    monkeypatch.setattr(codex_job_module, "_manager", manager)
    service._update_codex_verification(
        continuation,
        build_run_event(
            run_id=continuation.id,
            event_type="started",
            chat_id="chat-1",
        ),
    )
    assert child_store.get("job-1")["display_status"] == "verifying_changes"  # type: ignore[index]
    service._update_codex_verification(
        continuation,
        build_run_event(
            run_id=continuation.id,
            event_type="completed",
            chat_id="chat-1",
        ),
    )
    record = child_store.get("job-1")
    assert record is not None
    assert record["verification_status"] == "completed"
    assert record["display_status"] == "completed"


def test_chat_codex_workdir_cannot_escape_workspace(tmp_path: Path) -> None:
    tokens = runtime_context.push_chat_runtime_context("containment-test", "run-1")
    try:
        with pytest.raises(ValueError, match="inside the workspace"):
            runtime_context.resolve_codex_workdir(tmp_path)
    finally:
        runtime_context.pop_chat_runtime_context(tokens)


def test_chat_codex_workdir_rejects_parent_path_guessing() -> None:
    tokens = runtime_context.push_chat_runtime_context("containment-test", "run-1")
    try:
        with pytest.raises(ValueError, match="parent path segments"):
            runtime_context.resolve_codex_workdir("../../workspace")
    finally:
        runtime_context.pop_chat_runtime_context(tokens)


def test_apps_paths_resolve_from_workspace_root() -> None:
    tokens = runtime_context.push_chat_runtime_context("containment-test", "run-1")
    try:
        resolved = runtime_context.resolve_codex_workdir("apps/app_cloud123")
        assert resolved == runtime_context.ensure_workspace_dir().resolve() / (
            "apps/app_cloud123"
        )
    finally:
        runtime_context.pop_chat_runtime_context(tokens)


def test_completed_codex_run_injects_verification_context(monkeypatch) -> None:
    store = CodexRunStore(":memory:")
    _create_record(store)
    store.update("job-1", status="done", result="Changed app.py", terminal=True)

    class _Manager:
        def __init__(self) -> None:
            self.store = store

        def list_for_session(self, session_id: str):
            return store.list_for_session(session_id)

    monkeypatch.setattr(chat_runner_module, "codex_job_manager", _Manager())
    run = Run(
        id="continuation-1",
        kind="chat",
        status="running",
        createdAt=1,
        updatedAt=1,
        chatId="chat-1",
        sourceId="codex:job-1",
        turnId="done",
    )

    messages = chat_runner_module._codex_context_messages(run)
    assert len(messages) == 1
    assert "Changed app.py" in str(messages[0].content)
    assert "run proportionate verification" in str(messages[0].content)


def test_runs_for_same_chat_are_serialized() -> None:
    service = RunsService(store=None, broadcaster=None)  # type: ignore[arg-type]
    active = 0
    maximum = 0

    class _Runner:
        kind = "chat"

        async def execute(self, run, runs_service) -> None:
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1

    def make_run(run_id: str) -> Run:
        return Run(
            id=run_id,
            kind="chat",
            status="running",
            createdAt=1,
            updatedAt=1,
            chatId="chat-1",
        )

    async def exercise() -> None:
        runner = _Runner()
        await asyncio.gather(
            service._execute_serialized(runner, make_run("run-1")),
            service._execute_serialized(runner, make_run("run-2")),
        )

    asyncio.run(exercise())
    assert maximum == 1
    assert service._chat_locks == {}
    assert service._chat_lock_users == {}
