from __future__ import annotations

from server.types.run import Run, RunEvent
from server.types.ws import RunAcceptedWSEnvelope, RunEventWSEnvelope
from server.ws.manager import ConnectionManager, connection_manager


class RunBroadcaster:
    def __init__(self, manager: ConnectionManager = connection_manager) -> None:
        self._manager = manager

    async def broadcast_run_accepted(self, run: Run) -> None:
        await self._manager.broadcast(
            RunAcceptedWSEnvelope(
                type="run.accepted",
                data=run,
            )
        )

    async def broadcast_run_event(self, event: RunEvent) -> None:
        await self._manager.broadcast(
            RunEventWSEnvelope(
                type="run.event",
                data=event,
            )
        )
