"""LAN access control for the box.

After pairing, the box and the desktop app share a `local_token` (see
`server/pairing.py`). Direct LAN calls must present it, so an unpaired box —
or a random peer on the same network — cannot drive it. Public endpoints
(`/health`, `/device/info`, `/pair`) stay open because they carry no sensitive
data and are needed to bootstrap pairing.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException

from aios_core.db import get_device_link


def _expected_local_token() -> str | None:
    link = get_device_link()
    return link["local_token"] if link else None


def _matches(provided: str | None, expected: str | None) -> bool:
    if not expected or not provided:
        return False
    return secrets.compare_digest(provided, expected)


async def require_local_token(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency guarding LAN HTTP routes."""
    expected = _expected_local_token()
    if not expected:
        raise HTTPException(status_code=401, detail="Device is not paired")

    provided = authorization
    if provided and provided.startswith("Bearer "):
        provided = provided.split(" ", 1)[1]

    if not _matches(provided, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing device token")


def is_valid_ws_token(token: str | None) -> bool:
    """Token check for the WebSocket handshake (token arrives as a query param,
    since the browser WebSocket API can't set headers)."""
    return _matches(token, _expected_local_token())
