"""BLE WiFi provisioning for a fresh, off-network box ("true boot").

On boot, if the box has no network, `main.py` runs this so a phone can hand it
WiFi credentials over Bluetooth Low Energy. The box advertises a GATT service
(BlueZ, via `bless`), receives the credentials, joins WiFi with `nmcli`, and
returns — after which the normal server starts and the box becomes discoverable
and pairable exactly as an always-on-WiFi box would.

GATT contract — MUST stay in sync with the mobile app
(aios-mobile `src/constants/provisioning.ts`):

    Service 7A71E000-1111-2222-3333-123456789ABC
      CREDS  7A71E001-...  write   <- phone writes {"ssid","password"} (JSON/utf-8)
      STATUS 7A71E002-...  notify  -> box pushes status lines:
                 credentials_received -> connecting -> connected
                                                     | failed:<reason>

`bless` picks the BlueZ backend on Linux automatically. Running the GATT server
needs BlueZ access, and `nmcli` needs privilege to change the network — on the
box this process runs as root (the Docker image / a root service), so both work.
"""
from __future__ import annotations

import asyncio
import json
import subprocess

from bless import (  # type: ignore
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions,
)

SERVICE_UUID = "7A71E000-1111-2222-3333-123456789ABC"
CREDS_UUID = "7A71E001-1111-2222-3333-123456789ABC"
STATUS_UUID = "7A71E002-1111-2222-3333-123456789ABC"


async def _make_adapter_pairable() -> None:
    """Ensure the BLE adapter accepts bonding.

    Our characteristics require an encrypted link, so iOS bonds before writing
    credentials. If the adapter is `Pairable: no` (a common default) that bond is
    refused and the encrypted write fails. Best-effort — never blocks provisioning.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "pairable", "on",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        await proc.communicate()
    except Exception as exc:  # noqa: BLE001 - best-effort
        print(f"[provisioning] could not set adapter pairable: {exc}")


async def _nmcli_connect(ssid: str, password: str) -> None:
    """Join a WiFi network with NetworkManager. Raises on failure."""
    proc = await asyncio.create_subprocess_exec(
        "nmcli", "dev", "wifi", "connect", ssid, "password", password,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, err = await proc.communicate()
    combined = ((out or b"") + b" " + (err or b"")).decode().strip()
    print(f"[provisioning] nmcli rc={proc.returncode} {combined!r}")
    if proc.returncode != 0:
        raise RuntimeError(combined or "unknown_error")


def _classify_failure(message: str) -> str:
    low = message.lower()
    if "secrets were required" in low or "802-11-wireless-security" in low or "password" in low:
        return "incorrect_password"
    if "no network with ssid" in low or "not found" in low:
        return "network_not_found"
    return message.splitlines()[0][:120] if message else "unknown"


async def run_provisioning(device_id: str, name: str | None = None) -> bool:
    """Advertise over BLE and block until the box is handed working WiFi
    credentials and joins the network. Returns True once online.

    Repeated/failed attempts keep the server advertising so the user can retry
    (e.g. after a wrong password) without a reboot.
    """
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    server = BlessServer(name=name or f"aios-{device_id[:8]}", loop=loop)

    def push(text: str) -> None:
        char = server.get_characteristic(STATUS_UUID)
        char.value = text.encode("utf-8")
        server.update_value(SERVICE_UUID, STATUS_UUID)
        print(f"[provisioning] status -> {text}")

    async def handle_credentials(raw: bytes) -> None:
        try:
            creds = json.loads(bytes(raw))
            ssid = creds["ssid"]
            password = creds.get("password", "")
        except Exception as exc:  # malformed payload from the phone
            print(f"[provisioning] bad payload: {exc!r}")
            push("failed:bad_payload")
            return
        print(f"[provisioning] received SSID {ssid!r} (password hidden)")
        push("credentials_received")
        push("connecting")
        try:
            await _nmcli_connect(ssid, password)
            push("connected")
            done.set()
        except Exception as exc:
            push(f"failed:{_classify_failure(str(exc))}")

    def on_write(characteristic: BlessGATTCharacteristic, value, **kwargs) -> None:
        if characteristic.uuid.lower() == CREDS_UUID.lower():
            asyncio.run_coroutine_threadsafe(handle_credentials(bytes(value)), loop)

    def on_read(characteristic: BlessGATTCharacteristic, **kwargs):
        return characteristic.value

    server.read_request_func = on_read
    server.write_request_func = on_write

    await server.add_new_service(SERVICE_UUID)
    # NOTE: link encryption (bonding) is temporarily reverted. Requiring encrypted
    # characteristics works, but needs three things together: adapter pairable + a
    # registered NoInputNoOutput pairing agent + the PHONE retrying the first write
    # (which triggers the bond and fails once) without tearing down the connection.
    # Re-enable by switching these back to *_encryption_required once the phone
    # ships the write-retry and the box registers the agent in-process. See
    # _make_adapter_pairable() (kept) and the memory note.
    await server.add_new_characteristic(
        SERVICE_UUID,
        CREDS_UUID,
        GATTCharacteristicProperties.write,
        None,
        GATTAttributePermissions.writeable,
    )
    # STATUS is notify+read, so it must start with no cached value.
    await server.add_new_characteristic(
        SERVICE_UUID,
        STATUS_UUID,
        GATTCharacteristicProperties.read | GATTCharacteristicProperties.notify,
        None,
        GATTAttributePermissions.readable,
    )

    await server.start()
    await _make_adapter_pairable()
    print(f"[provisioning] advertising {SERVICE_UUID} as {server.name!r}; awaiting WiFi")
    try:
        await done.wait()
        # Let the phone receive the terminal "connected" status before we tear
        # down the BLE server. Stopping it disconnects the phone, which otherwise
        # races ahead of the final notification and shows a false failure even
        # though the box joined WiFi fine.
        await asyncio.sleep(1.5)
    finally:
        try:
            await server.stop()
        except Exception:
            pass
    return True
