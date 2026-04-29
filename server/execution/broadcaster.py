from __future__ import annotations

from server.types.run import Run, RunEvent
from server.types.ws import RunAcceptedWSEnvelope, RunEventWSEnvelope
from server.ws.manager import ConnectionManager, connection_manager


class RunBroadcaster:
    def __init__(self, manager: ConnectionManager = connection_manager) -> None:
        self._manager = manager

    async def broadcast_run_accepted(self, run: Run) -> None:
        envelope = RunAcceptedWSEnvelope(
            type="run.accepted",
            data=run,
        )

        if run.userId:
            await self._manager.broadcast_to_user(run.userId, envelope)

        return

    async def broadcast_run_event(self, event: RunEvent) -> None:
        envelope = RunEventWSEnvelope(
            type="run.event",
            data=event,
        )

        if event.userId:
            await self._manager.broadcast_to_user(event.userId, envelope)

        return
