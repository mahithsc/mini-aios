from __future__ import annotations

import asyncio
import logging
import os

from server.execution.runtime import get_runs_service
from server.types.run import RunCreateRequest

log = logging.getLogger(__name__)
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 120


def _env_flag(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() not in {"0", "false", "no", "off"}


class HeartbeatScheduler:
    def __init__(
        self,
        *,
        interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        enabled: bool = True,
    ) -> None:
        self.interval_seconds = max(1, int(interval_seconds))
        self.enabled = enabled
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self.enabled:
            log.info("Heartbeat scheduler is disabled")
            return

        if self._task is not None and not self._task.done():
            return

        self._task = asyncio.create_task(self._run_loop(), name="heartbeat-scheduler")

    async def shutdown(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run_loop(self) -> None:
        while True:
            try:
                await self._submit_if_idle()
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Heartbeat scheduler tick failed")
                await asyncio.sleep(self.interval_seconds)

    async def _submit_if_idle(self) -> None:
        runs_service = get_runs_service()
        active_heartbeats = runs_service.list_active_runs(kinds=["heartbeat"], limit=1)
        if active_heartbeats:
            log.info("Skipping heartbeat tick because a heartbeat run is already active")
            return

        run = await runs_service.submit_run(RunCreateRequest(kind="heartbeat"))
        log.info("Submitted heartbeat run %s", run.id)


heartbeat_scheduler = HeartbeatScheduler(
    interval_seconds=int(os.getenv("AIOS_HEARTBEAT_INTERVAL_SECONDS", DEFAULT_HEARTBEAT_INTERVAL_SECONDS)),
    enabled=_env_flag("AIOS_HEARTBEAT_ENABLED", True),
)


async def start_heartbeat_scheduler() -> HeartbeatScheduler:
    await heartbeat_scheduler.start()
    return heartbeat_scheduler


async def shutdown_heartbeat_scheduler() -> None:
    await heartbeat_scheduler.shutdown()
