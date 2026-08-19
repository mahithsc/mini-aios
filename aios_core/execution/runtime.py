from __future__ import annotations

import logging
import os

from aios_core.conversation_store import ConversationStore
from aios_core.execution.runners.chat import ActivityIndicator, ChatRunner
from aios_core.execution.service import RunEventBroadcaster, RunsService
from aios_core.execution.store import FileRunStore, SQLiteRunStore

_runs_service: RunsService | None = None
log = logging.getLogger(__name__)


def _resolve_worker_count() -> int:
    raw_value = os.getenv("AIOS_RUN_WORKERS")
    if raw_value is not None:
        try:
            return max(1, int(raw_value))
        except ValueError:
            pass

    cpu_count = os.cpu_count() or 1
    return max(2, min(8, cpu_count))


def initialize_runs_service(
    *,
    broadcaster: RunEventBroadcaster,
    activity: ActivityIndicator | None = None,
) -> RunsService:
    global _runs_service
    if _runs_service is None:
        store = SQLiteRunStore()
        try:
            store.import_file_store(FileRunStore(create_directories=False))
        except Exception:
            # Legacy import is best-effort. SQLite is authoritative as soon as
            # a run exists there, and old files are never allowed to overwrite
            # newer database state on a later startup.
            log.exception("Failed to import legacy file-backed runs")
        _runs_service = RunsService(
            store=store,
            broadcaster=broadcaster,
            worker_count=_resolve_worker_count(),
            conversation_store=ConversationStore(),
        )
        _runs_service.register_runner(ChatRunner(activity=activity))
    return _runs_service


def get_runs_service() -> RunsService:
    if _runs_service is None:
        raise RuntimeError("RunsService has not been initialized.")
    return _runs_service


async def start_runs_service(
    *,
    broadcaster: RunEventBroadcaster,
    activity: ActivityIndicator | None = None,
) -> RunsService:
    service = initialize_runs_service(
        broadcaster=broadcaster,
        activity=activity,
    )
    await service.start()
    return service


async def shutdown_runs_service() -> None:
    global _runs_service
    if _runs_service is None:
        return
    await _runs_service.shutdown()
    _runs_service = None
