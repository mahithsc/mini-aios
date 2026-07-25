"""Cloudflare Tunnel runner.

After pairing, the cloud provisions a per-device tunnel and hands the box a
`connector_token` (stored in `device_link`). Running `cloudflared` with that
token brings up the box's public `<name>.trywink.io` subdomain so the desktop
can reach it directly when off-LAN — the box dials out to Cloudflare, so no
port-forwarding or public IP is needed.
"""

from __future__ import annotations

import subprocess

from aios_core.db import get_device_link

_process: subprocess.Popen | None = None


def start_cloudflared(token: str) -> None:
    """Run cloudflared with the connector token (idempotent)."""
    global _process
    if _process is not None and _process.poll() is None:
        return  # already running
    try:
        _process = subprocess.Popen(
            ["cloudflared", "tunnel", "run", "--token", token],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("[tunnel] cloudflared started")
    except FileNotFoundError:
        print("[tunnel] cloudflared binary not found; box is LAN-only")


def start_if_paired() -> None:
    """Start the tunnel on boot if this box is already paired with a token."""
    link = get_device_link()
    if link and link.get("connector_token"):
        start_cloudflared(link["connector_token"])


def stop_cloudflared() -> None:
    global _process
    if _process is not None:
        _process.terminate()
        _process = None
