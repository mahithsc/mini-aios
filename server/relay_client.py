"""Outbound relay connection to aios-cloud.

Since the box sits behind home NAT, it cannot be reached directly from outside.
Instead it dials out and holds a WebSocket open to the cloud's `/ws/device`,
authenticated with its device token. Commands arrive over that socket and are
handled by the same logic as the LAN `/command` endpoint.

The client self-activates once the box is paired (polls for the device link),
reconnects with a fixed backoff, and sends periodic heartbeats.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket

import websockets

from aios_core.db import get_device_link
from server.commands import handle_device_command
from server.discovery import _primary_lan_ip

_RECONNECT_DELAY = 3.0
_UNPAIRED_POLL = 2.0
_HEARTBEAT_INTERVAL = 30.0


def _cloud_ws_url(token: str) -> str:
    base = os.getenv("AIOS_CLOUD_URL", "https://computer.trywink.io")
    if base.startswith("https"):
        ws_base = "wss" + base[len("https") :]
    elif base.startswith("http"):
        ws_base = "ws" + base[len("http") :]
    else:
        ws_base = base
    return f"{ws_base}/ws/device?token={token}"


class RelayClient:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            link = get_device_link()
            if not link:
                await asyncio.sleep(_UNPAIRED_POLL)
                continue
            try:
                # Force IPv4: the cloud (Cloudflare) publishes AAAA records, and
                # a dead IPv6 route would hang the handshake until it times out.
                # `family` is forwarded to loop.create_connection and works under
                # uvloop (unlike happy_eyeballs_delay, which uvloop rejects).
                async with websockets.connect(
                    _cloud_ws_url(link["device_token"]),
                    open_timeout=20,
                    family=socket.AF_INET,
                ) as ws:
                    print("[relay] connected to cloud")
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[relay] connection error: {exc}")
            if not self._stopping:
                await asyncio.sleep(_RECONNECT_DELAY)

    def _heartbeat_payload(self) -> str:
        # Report our current LAN IP so the cloud can offer it for LAN-only
        # pairing on networks where mDNS is blocked (guest/corporate Wi-Fi).
        return json.dumps({"type": "heartbeat", "lan_ip": _primary_lan_ip()})

    async def _session(self, ws) -> None:
        # Report our public tunnel URL immediately so the cloud can hand it to
        # the desktop without waiting for the first heartbeat interval.
        await ws.send(self._heartbeat_payload())
        heartbeat = asyncio.create_task(self._heartbeat(ws))
        try:
            async for raw in ws:
                await self._handle_message(ws, raw)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _handle_message(self, ws, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (ValueError, TypeError):
            return
        request_id = message.get("request_id")
        if not request_id:
            return
        try:
            result = handle_device_command(message.get("type", ""), message.get("payload") or {})
            reply = {"request_id": request_id, "ok": True, "result": result}
        except Exception as exc:
            reply = {"request_id": request_id, "ok": False, "error": str(exc)}
        await ws.send(json.dumps(reply))

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            await ws.send(self._heartbeat_payload())


relay_client = RelayClient()
