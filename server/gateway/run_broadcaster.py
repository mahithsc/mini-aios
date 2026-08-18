from __future__ import annotations

from server.gateway.bus import GatewayEventBus, get_gateway_bus
from server.types.run import Run, RunEvent


class RunBroadcaster:
    """Routes run lifecycle events into the gateway event bus (REST + SSE)."""

    def __init__(self, bus: GatewayEventBus | None = None) -> None:
        self._bus = bus or get_gateway_bus()

    async def broadcast_run_accepted(self, run: Run) -> None:
        await self._bus.handle_run_accepted(run)

    async def broadcast_run_event(self, event: RunEvent) -> None:
        await self._bus.handle_run_event(event)
