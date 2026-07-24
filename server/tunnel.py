"""ngrok tunnel — gives the box a public URL so the desktop app can reach it
directly when off-LAN (no cloud relay needed for the data path).

Runs the system `ngrok` agent as a subprocess and reads the assigned public URL
from ngrok's local API (127.0.0.1:4040). The URL is reported to the cloud over
the relay heartbeat so the desktop can look it up when it can't find the box on
the LAN.
"""

from __future__ import annotations

import asyncio
import subprocess

import httpx

_NGROK_API = "http://127.0.0.1:4040/api/tunnels"

_process: subprocess.Popen | None = None
_public_url: str | None = None


async def start_tunnel(port: int) -> str | None:
    global _process, _public_url
    if _public_url:
        return _public_url
    try:
        _process = subprocess.Popen(
            ["ngrok", "http", str(port), "--log=stdout"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        print("[tunnel] ngrok binary not found; skipping public tunnel")
        return None

    async with httpx.AsyncClient(timeout=3.0) as client:
        for _ in range(20):
            await asyncio.sleep(0.5)
            try:
                resp = await client.get(_NGROK_API)
                tunnels = resp.json().get("tunnels", [])
                url = next(
                    (t["public_url"] for t in tunnels if t["public_url"].startswith("https")),
                    None,
                )
                if url:
                    _public_url = url
                    print(f"[tunnel] public url: {url}")
                    return url
            except Exception:
                continue

    print("[tunnel] timed out waiting for ngrok public url")
    return None


def get_public_url() -> str | None:
    return _public_url


def stop_tunnel() -> None:
    global _process, _public_url
    if _process is not None:
        _process.terminate()
        _process = None
    _public_url = None
