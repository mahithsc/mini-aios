"""LAN discovery — advertise this box over mDNS so the desktop app can find
it on the local network without a hardcoded address.

The service is published under `_aios._tcp.local.` with the box's stable
`device_id` in the TXT record, so a client can identify *which* box it found
before pairing. Advertising is best-effort: if it fails (e.g. multicast
blocked on the network) the box still serves normally over its known port.
"""

from __future__ import annotations

import socket

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

AIOS_SERVICE_TYPE = "_aios._tcp.local."


def _primary_lan_ip() -> str:
    """Best-effort primary LAN IPv4 for this host.

    Opens a throwaway UDP socket toward a public address; no packets are sent,
    but the OS picks the outbound interface, whose local address is the LAN IP
    other machines can reach us on. Falls back to loopback.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class AiosDiscovery:
    """Advertises the box on the LAN via mDNS for the lifetime of the app."""

    def __init__(self, device_id: str, port: int, name: str | None = None) -> None:
        self._device_id = device_id
        self._port = port
        self._name = name or socket.gethostname()
        self._aiozc: AsyncZeroconf | None = None
        self._info: ServiceInfo | None = None

    async def start(self) -> None:
        instance = f"aios-{self._device_id[:8]}"
        self._info = ServiceInfo(
            AIOS_SERVICE_TYPE,
            f"{instance}.{AIOS_SERVICE_TYPE}",
            addresses=[socket.inet_aton(_primary_lan_ip())],
            port=self._port,
            properties={
                "device_id": self._device_id,
                "name": self._name,
                "version": "1",
            },
            server=f"{instance}.local.",
        )
        self._aiozc = AsyncZeroconf()
        await self._aiozc.async_register_service(self._info)

    async def stop(self) -> None:
        if self._aiozc is None:
            return
        try:
            if self._info is not None:
                await self._aiozc.async_unregister_service(self._info)
        finally:
            await self._aiozc.async_close()
            self._aiozc = None
            self._info = None
