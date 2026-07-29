from __future__ import annotations

import asyncio
import os
import sys

import uvicorn

_TRUTHY = {"1", "true", "yes", "on"}


def _is_online() -> bool:
    """True if the box has a usable (non-loopback) network interface.

    Reuses discovery's outbound-interface probe: with no network configured it
    can't pick an outbound interface and returns loopback.
    """
    from server.discovery import _primary_lan_ip

    return _primary_lan_ip() != "127.0.0.1"


def _ensure_network() -> None:
    """Boot gate: if the box is offline, run BLE WiFi provisioning until it
    joins a network, then return so the normal server starts.

    A no-op when already online (the common case), when disabled via
    AIOS_DISABLE_PROVISIONING, off-Linux, or when `bless` isn't installed.
    """
    if os.getenv("AIOS_DISABLE_PROVISIONING", "").strip().lower() in _TRUTHY:
        return
    if not sys.platform.startswith("linux"):
        return  # the provisioning gate is for the box (Linux/BlueZ) only
    # AIOS_FORCE_PROVISIONING runs the BLE setup even when already online, so the
    # provisioning path can be tested on a box that can't safely be taken offline.
    force = os.getenv("AIOS_FORCE_PROVISIONING", "").strip().lower() in _TRUTHY
    if _is_online() and not force:
        return

    # Imported lazily so an online box never needs `bless`/BlueZ present.
    try:
        from aios_core.db import get_or_create_device_id
        from server.provisioning import run_provisioning
    except Exception as exc:
        print(f"[provisioning] unavailable, starting server without setup: {exc}")
        return

    device_id = get_or_create_device_id()
    print("[provisioning] no network detected — starting BLE WiFi setup")
    try:
        asyncio.run(run_provisioning(device_id))
        print("[provisioning] box is online; continuing to server")
    except Exception as exc:
        print(f"[provisioning] error, starting server anyway: {exc}")


def main() -> None:
    _ensure_network()
    host = os.getenv("AIOS_SERVER_HOST", "0.0.0.0")
    port = int(os.getenv("AIOS_SERVER_PORT", "8765"))
    uvicorn.run("server.server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
