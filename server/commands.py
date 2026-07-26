"""Device commands.

A single handler serves both transports: the LAN HTTP `/command` endpoint and
the cloud relay's WebSocket. Keeping one implementation means "at home" and
"away" behave identically.
"""

from __future__ import annotations

import time

from aios_core.db import clear_device_link, get_or_create_device_id
from server.tunnel import stop_cloudflared


def handle_device_command(command_type: str, payload: dict) -> dict:
    """Dispatch a command and return its result. Raises ValueError for unknown
    commands (surfaced as a 400 on LAN / a failed relay reply off-LAN)."""
    if command_type == "ping":
        return {"pong": True, "device_id": get_or_create_device_id(), "ts": int(time.time())}

    if command_type == "unpair":
        # Cloud-authoritative revocation. When the owner revokes this device via
        # the cloud (DELETE /device/{id}), the cloud pushes this command down the
        # relay. Clear the local binding and drop the tunnel so the box goes idle
        # until re-paired — independent of the LAN local_token, which may be
        # stale (e.g. the box was re-paired elsewhere), the exact case that would
        # otherwise strand the box as `paired:true` with a token nobody holds.
        clear_device_link()
        stop_cloudflared()
        return {"unpaired": True, "device_id": get_or_create_device_id()}

    raise ValueError(f"Unknown command: {command_type}")
