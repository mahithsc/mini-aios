"""Device commands.

A single handler serves both transports: the LAN HTTP `/command` endpoint and
the cloud relay's WebSocket. Keeping one implementation means "at home" and
"away" behave identically.
"""

from __future__ import annotations

import time

from aios_core.db import get_or_create_device_id


def handle_device_command(command_type: str, payload: dict) -> dict:
    """Dispatch a command and return its result. Raises ValueError for unknown
    commands (surfaced as a 400 on LAN / a failed relay reply off-LAN)."""
    if command_type == "ping":
        return {"pong": True, "device_id": get_or_create_device_id(), "ts": int(time.time())}

    raise ValueError(f"Unknown command: {command_type}")
