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
_token: str | None = None


def _terminate() -> None:
    """Stop the running cloudflared and reap it (so it doesn't linger as a zombie)."""
    global _process, _token
    if _process is not None:
        _process.terminate()
        try:
            _process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _process.kill()
            _process.wait()
    _process = None
    _token = None


def start_cloudflared(token: str) -> None:
    """Run cloudflared with the connector token.

    Idempotent for the *same* token, but restarts when the token changes. A
    re-pair can hand the box a new connector token for a freshly provisioned
    tunnel; without restarting, the old cloudflared keeps serving a stale tunnel
    while DNS points at the new one, so Cloudflare can't reach the origin
    (error 1033).
    """
    global _process, _token
    if _process is not None and _process.poll() is None:
        if token == _token:
            return  # already running the right tunnel
        _terminate()  # token changed — drop the stale tunnel before relaunching
    try:
        _process = subprocess.Popen(
            ["cloudflared", "tunnel", "run", "--token", token],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _token = token
        print("[tunnel] cloudflared started")
    except FileNotFoundError:
        print("[tunnel] cloudflared binary not found; box is LAN-only")


def start_if_paired() -> None:
    """Start the tunnel on boot if this box is already paired with a token."""
    link = get_device_link()
    if link and link.get("connector_token"):
        start_cloudflared(link["connector_token"])


def stop_cloudflared() -> None:
    _terminate()
