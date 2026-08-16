from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Protocol

from aios_core.runtime_control import get_runtime_control
from server.execution.broadcaster import RunBroadcaster
from server.execution.store import RunStore
from server.types.run import Run, RunCreateRequest, RunEvent, RunEventType, RunKind, RunSnapshot, RunStatus

_STALE_RUN_ERROR_MESSAGE = "Server restarted before run completed."
log = logging.getLogger(__name__)


class RunExecutor(Protocol):
    kind: str

    async def execute(self, run: Run, runs_service: "RunsService") -> None:
        ...


class ConversationRecoveryStore(Protocol):
    def get_run_status(self, run_id: str) -> str | None:
        ...

    def recover_stale_run(self, run_id: str, *, error: str) -> bool:
        ...

    def recover_stale_runs(
        self,
        *,
        error: str,
        chat_id: str | None = None,
    ) -> list[str]:
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
        conversation_store: ConversationRecoveryStore | None = None,
    ) -> None:
        self._store = store
        self._broadcaster = broadcaster
        self._worker_count = max(1, worker_count)
        self._conversation_store = conversation_store
        self._active_runs: dict[str, ActiveRun] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._runners: dict[str, RunExecutor] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        if self._started:
            return

        # Canonical SQLite can remain nonterminal even when a cleanup failure
        # already made the file-backed run snapshot terminal. Reconcile it
        # independently before consulting the run projection.
        if self._conversation_store is not None:
            await asyncio.to_thread(
                self._conversation_store.recover_stale_runs,
                error=_STALE_RUN_ERROR_MESSAGE,
            )
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
        get_runtime_control().ensure_accepting_work()
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
        # Include terminal chat projections for canonical-status repair and
        # non-chat queued/running runs for the existing restart behavior.
        snapshots = self._store.list_snapshots()
        for snapshot in snapshots:
            run = self._store.get_run(snapshot.runId)
            if run is None:
                continue

            canonical_status: str | None = None
            if (
                run.kind == "chat"
                and run.chatId
                and self._conversation_store is not None
            ):
                if snapshot.status in {"queued", "running"}:
                    # The file-backed run log and canonical SQLite history have
                    # separate projections. Repair SQLite first so a restart can
                    # never expose an orphan call or replay a side effect blindly.
                    await asyncio.to_thread(
                        self._conversation_store.recover_stale_run,
                        snapshot.runId,
                        error=_STALE_RUN_ERROR_MESSAGE,
                    )
                canonical_status = await asyncio.to_thread(
                    self._conversation_store.get_run_status,
                    snapshot.runId,
                )

            terminal = self._canonical_terminal_projection(canonical_status)
            if terminal is not None:
                event_type, expected_status, data = terminal
                if snapshot.status != expected_status:
                    await self.emit_event(
                        snapshot.runId,
                        build_run_event(
                            run_id=snapshot.runId,
                            event_type=event_type,
                            chat_id=run.chatId,
                            data={**data, "recovered": True},
                        ),
                    )
                else:
                    # A previous terminal event may have reached the file
                    # snapshot but failed before updating the SQL chat
                    # projection. Reapply the persisted event; assistant-event
                    # IDs make this operation idempotent.
                    await self._restore_canonical_terminal_projection(
                        run,
                        canonical_status or "",
                        broadcast_existing=False,
                    )
                continue

            if snapshot.status in {"queued", "running"}:
                await self.emit_event(
                    snapshot.runId,
                    build_run_event(
                        run_id=snapshot.runId,
                        event_type="error",
                        chat_id=run.chatId,
                        data={"error": _STALE_RUN_ERROR_MESSAGE},
                    ),
                )

    @staticmethod
    def _canonical_terminal_projection(
        canonical_status: str | None,
    ) -> tuple[RunEventType, RunStatus, dict[str, object]] | None:
        if canonical_status == "complete":
            return "completed", "completed", {}
        if canonical_status == "cancelled":
            return (
                "cancelled",
                "cancelled",
                {"reason": "Canonical conversation turn was cancelled."},
            )
        if canonical_status == "error":
            return "error", "error", {"error": _STALE_RUN_ERROR_MESSAGE}
        return None

    async def _canonical_status_for_run(self, run: Run) -> str | None:
        if (
            run.kind != "chat"
            or not run.chatId
            or self._conversation_store is None
        ):
            return None
        try:
            return await asyncio.to_thread(
                self._conversation_store.get_run_status,
                run.id,
            )
        except Exception:
            log.exception("Failed to read canonical status for run %s", run.id)
            return None

    async def _restore_canonical_terminal_projection(
        self,
        run: Run,
        canonical_status: str,
        *,
        broadcast_existing: bool = True,
    ) -> None:
        terminal = self._canonical_terminal_projection(canonical_status)
        if terminal is None:
            return
        event_type, expected_status, data = terminal
        snapshot = self._store.get_snapshot(run.id)
        if snapshot is not None and snapshot.status == expected_status:
            # The file event may already have committed before its chat/SSE
            # projection failed. Replay that exact event idempotently instead
            # of appending a contradictory error terminal.
            events = self._store.list_events_after(
                run.id,
                max(0, snapshot.lastSequence - 1),
            )
            existing = next(
                (
                    event
                    for event in reversed(events)
                    if event.event.type == event_type
                ),
                None,
            )
            if existing is not None:
                if run.chatId:
                    self._store.project_chat_state(run.id, run.chatId, existing)
                if broadcast_existing:
                    await self._broadcaster.broadcast_run_event(existing)
                self._sync_active_run(snapshot)
                return

        await self.emit_event(
            run.id,
            build_run_event(
                run_id=run.id,
                event_type=event_type,
                chat_id=run.chatId,
                data=data,
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
                        try:
                            await self.emit_event(
                                run_id,
                                build_run_event(
                                    run_id=run_id,
                                    event_type="cancelled",
                                    chat_id=active.run.chatId,
                                    data={"reason": "Run stopped by user."},
                                ),
                            )
                        except Exception:
                            canonical_status = await self._canonical_status_for_run(
                                active.run
                            )
                            try:
                                await self._restore_canonical_terminal_projection(
                                    active.run,
                                    canonical_status or "",
                                )
                            except Exception:
                                # A secondary cancellation projection failure
                                # must not kill this worker or alter canonical
                                # state. Startup reconciliation retries it.
                                log.exception(
                                    "Failed to restore cancelled projection for %s",
                                    run_id,
                                )
                        continue
                    if active.execution_task is not None:
                        active.execution_task.cancel()
                    raise
                except Exception as exc:
                    canonical_status = await self._canonical_status_for_run(active.run)
                    if self._canonical_terminal_projection(canonical_status) is not None:
                        try:
                            await self._restore_canonical_terminal_projection(
                                active.run,
                                canonical_status or "",
                            )
                        except Exception:
                            # Never downgrade a canonically terminal turn to an
                            # error because its secondary projection failed.
                            # Startup reconciliation will retry the projection.
                            log.exception(
                                "Failed to restore canonical terminal projection for %s",
                                run_id,
                            )
                        continue
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
