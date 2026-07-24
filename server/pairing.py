"""Box side of the pairing handshake.

The desktop app (authenticated as the user) obtains a short-lived pairing code
from aios-cloud, then hands it to this box over the LAN. The box redeems the
code with the cloud, receives its long-lived device token, and generates a
`local_token` — the shared secret the desktop uses for direct LAN calls.
"""

from __future__ import annotations

import os
import secrets
import time

import httpx

from aios_core.db import get_or_create_device_id, save_device_link


class PairingError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def cloud_url() -> str:
    return os.getenv("AIOS_CLOUD_URL", "https://computer.trywink.io")


async def complete_pairing(pairing_code: str) -> dict[str, object]:
    device_id = get_or_create_device_id()

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{cloud_url()}/device/claim/complete",
                json={"device_id": device_id, "pairing_code": pairing_code},
            )
    except httpx.HTTPError as exc:
        raise PairingError(502, f"Could not reach aios-cloud: {exc}") from exc

    if resp.status_code != 200:
        detail = "Pairing failed"
        try:
            body = resp.json()
            if isinstance(body.get("detail"), str):
                detail = body["detail"]
        except Exception:
            pass
        raise PairingError(resp.status_code, detail)

    data = resp.json()
    local_token = secrets.token_urlsafe(32)

    save_device_link(
        device_token=data["device_token"],
        local_token=local_token,
        owner_user_id=data["user"]["id"],
        owner_email=data["user"]["email"],
        slug=data["slug"],
        paired_at=int(time.time()),
    )

    return {
        "status": "paired",
        "device_id": device_id,
        "local_token": local_token,
        "owner_email": data["user"]["email"],
        "slug": data["slug"],
    }
