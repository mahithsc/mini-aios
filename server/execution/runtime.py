from __future__ import annotations

import os

from aios_core.conversation_store import ConversationStore
from server.execution.broadcaster import RunBroadcaster
from server.execution.runners.chat import ChatRunner
from server.execution.service import RunsService
from server.execution.store import FileRunStore

_runs_service: RunsService | None = None


def _resolve_worker_count() -> int:
    raw_value = os.getenv("AIOS_RUN_WORKERS")
    if raw_value is not None:
        try:
            return max(1, int(raw_value))
        except ValueError:
            pass

    cpu_count = os.cpu_count() or 1
    return max(2, min(8, cpu_count))


def initialize_runs_service() -> RunsService:
    global _runs_service
    if _runs_service is None:
        _runs_service = RunsService(
            store=FileRunStore(),
            broadcaster=RunBroadcaster(),
            worker_count=_resolve_worker_count(),
            conversation_store=ConversationStore(),
        )
        _runs_service.register_runner(ChatRunner())
    return _runs_service


def get_runs_service() -> RunsService:
    if _runs_service is None:
        raise RuntimeError("RunsService has not been initialized.")
    return _runs_service


async def start_runs_service() -> RunsService:
    service = initialize_runs_service()
    await service.start()
    return service


async def shutdown_runs_service() -> None:
    global _runs_service
    if _runs_service is None:
        return
    await _runs_service.shutdown()
    _runs_service = None
