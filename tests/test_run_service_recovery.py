from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aios_core.execution.service import ActiveRun, RunsService, build_run_event
from server.types.run import Run, RunSnapshot


class _EmptyRunStore:
    def list_snapshots(self, **_kwargs):
        return []


class _RecoveryStore:
    def __init__(self, status: str | None = None) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.per_run_calls: list[tuple[str, str]] = []
        self.status = status

    def get_run_status(self, _run_id: str) -> str | None:
        return self.status

    def recover_stale_runs(
        self,
        *,
        error: str,
        chat_id: str | None = None,
    ) -> list[str]:
        self.calls.append((error, chat_id))
        return []

    def recover_stale_run(self, run_id: str, *, error: str) -> bool:
        self.per_run_calls.append((run_id, error))
        return False


def test_service_start_recovers_canonical_history_before_workers() -> None:
    recovery = _RecoveryStore()
    service = RunsService(
        store=_EmptyRunStore(),
        broadcaster=SimpleNamespace(),
        worker_count=1,
        conversation_store=recovery,
    )

    async def exercise() -> None:
        await service.start()
        await service.shutdown()

    asyncio.run(exercise())

    assert recovery.calls == [("Server restarted before run completed.", None)]


class _StaleRunStore:
    def __init__(self) -> None:
        self.hide_snapshots = False
        self.run = Run(
            id="run-stale",
            kind="chat",
            status="running",
            createdAt=1,
            updatedAt=1,
            chatId="chat-1",
            turnId="turn-1",
        )
        self.snapshot = RunSnapshot(
            runId="run-stale",
            kind="chat",
            status="running",
            updatedAt=1,
            chatId="chat-1",
            lastSequence=0,
        )
        self.events = []
        self.projections = []

    def list_snapshots(self, **_kwargs):
        return [] if self.hide_snapshots else [self.snapshot]

    def get_run(self, run_id, **_kwargs):
        return self.run if run_id == self.run.id else None

    def append_event(self, _run_id, event):
        persisted = event.model_copy(update={"sequence": len(self.events) + 1})
        self.events.append(persisted)
        return persisted

    def save_snapshot(self, run_id, *, status, last_sequence, **kwargs):
        self.snapshot = self.snapshot.model_copy(
            update={
                "runId": run_id,
                "status": status,
                "lastSequence": last_sequence,
                "preview": kwargs.get("preview"),
                "activeStep": kwargs.get("active_step"),
            }
        )
        return self.snapshot

    def project_chat_state(self, *_args, **_kwargs) -> None:
        self.projections.append((_args, _kwargs))

    def get_snapshot(self, _run_id):
        return self.snapshot

    def list_events_after(self, _run_id, sequence, **_kwargs):
        return [event for event in self.events if event.sequence > sequence]


class _Broadcaster:
    def __init__(self) -> None:
        self.events = []

    async def broadcast_run_event(self, event) -> None:
        self.events.append(event)


def test_file_projection_recovers_canonical_completed_status() -> None:
    store = _StaleRunStore()
    recovery = _RecoveryStore(status="complete")
    broadcaster = _Broadcaster()
    service = RunsService(
        store=store,
        broadcaster=broadcaster,
        worker_count=1,
        conversation_store=recovery,
    )

    async def exercise() -> None:
        await service.start()
        await service.shutdown()

    asyncio.run(exercise())

    assert recovery.per_run_calls == [
        ("run-stale", "Server restarted before run completed.")
    ]
    assert [event.event.type for event in broadcaster.events] == ["completed"]
    assert store.snapshot.status == "completed"


def test_terminal_file_error_is_repaired_from_canonical_completion() -> None:
    store = _StaleRunStore()
    store.snapshot = store.snapshot.model_copy(update={"status": "error"})
    recovery = _RecoveryStore(status="complete")
    broadcaster = _Broadcaster()
    service = RunsService(
        store=store,
        broadcaster=broadcaster,
        worker_count=1,
        conversation_store=recovery,
    )

    async def exercise() -> None:
        await service.start()
        await service.shutdown()

    asyncio.run(exercise())

    assert recovery.per_run_calls == []
    assert [event.event.type for event in broadcaster.events] == ["completed"]
    assert store.snapshot.status == "completed"


def test_matching_terminal_snapshot_reapplies_sql_projection() -> None:
    store = _StaleRunStore()
    completed = build_run_event(
        run_id="run-stale",
        event_type="completed",
        chat_id="chat-1",
    ).model_copy(update={"sequence": 1, "kind": "chat"})
    store.events = [completed]
    store.snapshot = store.snapshot.model_copy(
        update={"status": "completed", "lastSequence": 1}
    )
    recovery = _RecoveryStore(status="complete")
    broadcaster = _Broadcaster()
    service = RunsService(
        store=store,
        broadcaster=broadcaster,
        worker_count=1,
        conversation_store=recovery,
    )

    async def exercise() -> None:
        await service.start()
        await service.shutdown()

    asyncio.run(exercise())

    assert len(store.projections) == 1
    assert broadcaster.events == []


def test_worker_error_cannot_downgrade_canonical_completion() -> None:
    store = _StaleRunStore()
    store.hide_snapshots = True
    recovery = _RecoveryStore()
    broadcaster = _Broadcaster()
    service = RunsService(
        store=store,
        broadcaster=broadcaster,
        worker_count=1,
        conversation_store=recovery,
    )

    class _Runner:
        kind = "chat"

        async def execute(self, _run, _service) -> None:
            recovery.status = "complete"
            raise RuntimeError("secondary projection failed")

    service.register_runner(_Runner())
    service._active_runs[store.run.id] = ActiveRun(run=store.run)

    async def exercise() -> None:
        await service.start()
        await service._queue.put(store.run.id)
        await service._queue.join()
        await service.shutdown()

    asyncio.run(exercise())

    assert [event.event.type for event in broadcaster.events] == ["completed"]
    assert store.snapshot.status == "completed"
