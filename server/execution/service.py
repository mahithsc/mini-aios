from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

from server.execution.broadcaster import RunBroadcaster
from server.execution.store import RunStore
from server.types.run import Run, RunCreateRequest, RunEvent, RunEventType, RunKind, RunSnapshot, RunStatus

_STALE_RUN_ERROR_MESSAGE = "Server restarted before run completed."


class RunExecutor(Protocol):
    kind: str

    async def execute(self, run: Run, runs_service: "RunsService") -> None:
        ...


@dataclass(slots=True)
class ActiveRun:
    run: Run
    execution_task: asyncio.Task[None] | None = None
    cancel_requested: bool = False


class RunsService:
    def __init__(
        self,
        store: RunStore,
        broadcaster: RunBroadcaster,
        *,
        worker_count: int = 1,
    ) -> None:
        self._store = store
        self._broadcaster = broadcaster
        self._worker_count = max(1, worker_count)
        self._active_runs: dict[str, ActiveRun] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._runners: dict[str, RunExecutor] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return

        await self._reconcile_stale_runs()
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker_loop(index), name=f"runs-worker-{index}")
            for index in range(self._worker_count)
        ]

    async def shutdown(self) -> None:
        if not self._started:
            return

        for worker in self._workers:
            worker.cancel()

        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self._started = False

    def register_runner(self, runner: RunExecutor) -> None:
        self._runners[runner.kind] = runner

    async def submit_run(
        self,
        request: RunCreateRequest,
        *,
        user_id: str | None = None,
    ) -> Run:
        run = self._store.create_run(request, user_id=user_id)

        async with self._lock:
            self._active_runs[run.id] = ActiveRun(run=run)

        self._store.save_snapshot(run.id, status="queued", last_sequence=0)
        await self._broadcaster.broadcast_run_accepted(run)

        async with self._lock:
            await self._queue.put(run.id)
        return run

    def get_run(self, run_id: str, *, user_id: str | None = None) -> Run | None:
        active = self._active_runs.get(run_id)
        if active is not None:
            if user_id is not None and active.run.userId != user_id:
                return None
            return active.run
        return self._store.get_run(run_id, user_id=user_id)

    def list_active_runs(
        self,
        *,
        user_id: str | None = None,
        kinds: list[RunKind] | None = None,
        limit: int | None = None,
    ) -> list[RunSnapshot]:
        return self._store.list_snapshots(
            user_id=user_id,
            statuses=["queued", "running"],
            kinds=kinds,
            limit=limit,
        )

    def list_recent_runs(
        self,
        *,
        user_id: str | None = None,
        statuses: list[RunStatus] | None = None,
        kinds: list[RunKind] | None = None,
        limit: int | None = None,
    ) -> list[RunSnapshot]:
        return self._store.list_snapshots(
            user_id=user_id,
            statuses=statuses,
            kinds=kinds,
            limit=limit,
        )

    def get_snapshot(self, run_id: str) -> RunSnapshot | None:
        return self._store.get_snapshot(run_id)

    def resume_events(
        self,
        run_id: str,
        after_sequence: int,
        *,
        user_id: str | None = None,
    ) -> list[RunEvent]:
        return self._store.list_events_after(run_id, after_sequence, user_id=user_id)

    async def emit_event(self, run_id: str, event: RunEvent) -> RunEvent:
        run = self._store.get_run(run_id)
        if run is None:
            raise KeyError(f"Run {run_id} does not exist.")

        persisted_event = self._store.append_event(
            run_id,
            event.model_copy(
                update={
                    "kind": run.kind,
                    "userId": event.userId if event.userId is not None else run.userId,
                    "chatId": event.chatId if event.chatId is not None else run.chatId,
                }
            ),
        )
        snapshot = self._store.save_snapshot(
            run_id,
            status=self._derive_status(persisted_event),
            last_sequence=persisted_event.sequence,
            preview=self._derive_preview(persisted_event),
            active_step=self._derive_active_step(persisted_event),
        )
        if run is not None and run.chatId:
            self._store.project_chat_state(run_id, run.chatId, persisted_event)

        self._sync_active_run(snapshot)
        await self._broadcaster.broadcast_run_event(persisted_event)
        return persisted_event

    async def _reconcile_stale_runs(self) -> None:
        stale_snapshots = self._store.list_snapshots(statuses=["queued", "running"])
        for snapshot in stale_snapshots:
            run = self._store.get_run(snapshot.runId)
            if run is None:
                continue

            await self.emit_event(
                snapshot.runId,
                build_run_event(
                    run_id=snapshot.runId,
                    event_type="error",
                    chat_id=run.chatId,
                    data={"error": _STALE_RUN_ERROR_MESSAGE},
                ),
            )

    def mark_completed(self, run_id: str) -> RunSnapshot:
        existing_snapshot = self._store.get_snapshot(run_id)
        snapshot = self._store.save_snapshot(
            run_id,
            status="completed",
            last_sequence=self._get_last_sequence(run_id),
            preview=existing_snapshot.preview if existing_snapshot is not None else None,
            active_step=None,
        )
        self._sync_active_run(snapshot)
        return snapshot

    def mark_error(self, run_id: str, error: str | None = None) -> RunSnapshot:
        existing_snapshot = self._store.get_snapshot(run_id)
        snapshot = self._store.save_snapshot(
            run_id,
            status="error",
            last_sequence=self._get_last_sequence(run_id),
            preview=error or (existing_snapshot.preview if existing_snapshot is not None else None),
            active_step=None,
        )
        self._sync_active_run(snapshot)
        return snapshot

    def mark_cancelled(self, run_id: str, reason: str | None = None) -> RunSnapshot:
        existing_snapshot = self._store.get_snapshot(run_id)
        snapshot = self._store.save_snapshot(
            run_id,
            status="cancelled",
            last_sequence=self._get_last_sequence(run_id),
            preview=reason or (existing_snapshot.preview if existing_snapshot is not None else None),
            active_step=None,
        )
        self._sync_active_run(snapshot)
        return snapshot

    async def stop_run(self, run_id: str, *, user_id: str | None = None) -> bool:
        async with self._lock:
            active = self._active_runs.get(run_id)
            if active is None:
                return False

            if user_id is not None and active.run.userId != user_id:
                return False

            if active.cancel_requested:
                return True

            active.cancel_requested = True
            execution_task = active.execution_task

        if execution_task is None:
            await self.emit_event(
                run_id,
                build_run_event(
                    run_id=run_id,
                    event_type="cancelled",
                    chat_id=active.run.chatId,
                    data={"reason": "Run stopped by user."},
                ),
            )
            return True

        if execution_task.done():
            return False

        execution_task.cancel()
        return True

    async def _worker_loop(self, _: int) -> None:
        while True:
            run_id = await self._queue.get()
            try:
                active = self._active_runs.get(run_id)
                if active is None:
                    continue

                runner = self._runners.get(active.run.kind)
                if runner is None:
                    self.mark_error(run_id, error=f"No runner registered for run kind '{active.run.kind}'.")
                    continue

                if active.cancel_requested:
                    continue

                snapshot = self._store.save_snapshot(
                    run_id,
                    status="running",
                    last_sequence=self._get_last_sequence(run_id),
                    active_step="dispatch",
                )
                self._sync_active_run(snapshot)
                try:
                    if active.cancel_requested:
                        continue

                    execution_task = asyncio.create_task(
                        runner.execute(active.run, self),
                        name=f"run-execute-{run_id}",
                    )
                    active.execution_task = execution_task
                    await execution_task
                except asyncio.CancelledError:
                    if active.cancel_requested:
                        await self.emit_event(
                            run_id,
                            build_run_event(
                                run_id=run_id,
                                event_type="cancelled",
                                chat_id=active.run.chatId,
                                data={"reason": "Run stopped by user."},
                            ),
                        )
                        continue
                    if active.execution_task is not None:
                        active.execution_task.cancel()
                    raise
                except Exception as exc:
                    await self.emit_event(
                        run_id,
                        build_run_event(
                            run_id=run_id,
                            event_type="error",
                            chat_id=active.run.chatId,
                            data={"error": str(exc)},
                        ),
                    )
                finally:
                    if active.execution_task is not None and active.execution_task.done():
                        active.execution_task = None
            finally:
                self._queue.task_done()

    def _sync_active_run(self, snapshot: RunSnapshot) -> None:
        active = self._active_runs.get(snapshot.runId)
        if active is None:
            return

        if snapshot.status in {"completed", "error", "cancelled"}:
            self._active_runs.pop(snapshot.runId, None)
            return

        active.run = active.run.model_copy(
            update={
                "status": snapshot.status,
                "updatedAt": snapshot.updatedAt,
            }
        )

    def _derive_status(self, event: RunEvent) -> str:
        event_type = event.event.type
        if event_type == "completed":
            return "completed"
        if event_type == "error":
            return "error"
        if event_type == "cancelled":
            return "cancelled"
        return "running"

    def _derive_preview(self, event: RunEvent) -> str | None:
        data = event.event.data or {}
        value = data.get("value")
        if isinstance(value, str) and value.strip():
            return value[:160]

        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message[:160]

        error = data.get("error")
        if isinstance(error, str) and error.strip():
            return error[:160]

        return None

    def _derive_active_step(self, event: RunEvent) -> str | None:
        data = event.event.data or {}
        tool_name = data.get("toolName")
        if event.event.type == "tool_call_start" and isinstance(tool_name, str) and tool_name:
            return tool_name
        if event.event.type in {"completed", "error", "cancelled"}:
            return None
        return event.event.type

    def _get_last_sequence(self, run_id: str) -> int:
        snapshot = self._store.get_snapshot(run_id)
        if snapshot is not None:
            return snapshot.lastSequence
        return 0


def build_run_event(
    *,
    run_id: str,
    event_type: RunEventType,
    data: dict[str, object] | None = None,
    chat_id: str | None = None,
) -> RunEvent:
    return RunEvent(
        runId=run_id,
        sequence=0,
        createdAt=int(time.time() * 1000),
        chatId=chat_id,
        event={
            "type": event_type,
            "data": data,
        },
    )
