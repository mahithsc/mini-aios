"""BLE WiFi provisioning v2 for a fresh/off-network box ("true boot").

On boot with no network, `main.py` runs this so a phone can hand the box WiFi
credentials over Bluetooth. The BLE transport is PLAIN (no OS bonding — that
proved too finicky on headless BlueZ↔iOS); security is app-layer:

- **Re-provision** (box already claimed, e.g. moved to a new network): the phone
  proves it's the owner by encrypting the creds with the shared `provisioning_key`
  established at first claim (AES-256-GCM, AAD = a per-connection challenge → no
  replay). Only the key-holder can provision the box.
- **First claim** (no key yet): phone + box do an ephemeral X25519 ECDH → session
  key (protects the creds from passive sniffing). On success the box mints the
  persistent `provisioning_key`, returns it to the phone encrypted under the
  session key (KEYOUT), and both store it for future re-provisioning.

See aios-mobile/docs/ble-provisioning-v2.md. Crypto lives in provisioning_crypto.py
and is byte-for-byte interoperable with the phone's @noble implementation.

GATT (all plain; security is in the payload):
    Service 7A71E100-0000-1000-8000-00805F9B34FB
      DEVICE_INFO 7A71E101 read   -> {"device_id","claimed":bool,"slug"}
      PUBKEY      7A71E102 read   -> box ephemeral X25519 pubkey (hex) [claim]
      CHALLENGE   7A71E103 read   -> fresh 16-byte nonce (hex), regenerated per read
      CREDS       7A71E104 write  <- {"mode","phonePub"?,"blob"} (JSON; blob is sealed)
      STATUS      7A71E105 notify -> credentials_received|connecting|connected|failed:<reason>
      KEYOUT      7A71E107 read   -> provisioning_key sealed under session key (base64) [claim]
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess

from bless import (  # type: ignore
    BlessServer,
    BlessGATTCharacteristic,
    GATTCharacteristicProperties,
    GATTAttributePermissions,
)

from aios_core.db import get_device_link, get_provisioning_key, save_provisioning_key
from server.provisioning_crypto import (
    derive_session_key,
    generate_ephemeral,
    new_provisioning_key,
    open_credentials,
    seal,
)

SERVICE_UUID = "7A71E100-0000-1000-8000-00805F9B34FB"
DEVICE_INFO_UUID = "7A71E101-0000-1000-8000-00805F9B34FB"
PUBKEY_UUID = "7A71E102-0000-1000-8000-00805F9B34FB"
CHALLENGE_UUID = "7A71E103-0000-1000-8000-00805F9B34FB"
CREDS_UUID = "7A71E104-0000-1000-8000-00805F9B34FB"
STATUS_UUID = "7A71E105-0000-1000-8000-00805F9B34FB"
KEYOUT_UUID = "7A71E107-0000-1000-8000-00805F9B34FB"


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

    Failed attempts keep the server advertising so the user can retry.
    """
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    server = BlessServer(name=name or f"aios-{device_id[:8]}", loop=loop)

    provisioning_key = get_provisioning_key()  # bytes or None
    link = get_device_link()
    slug = link.get("slug") if link else None
    box_priv, box_pub = generate_ephemeral()  # ephemeral X25519 for first-claim
    state = {"challenge": os.urandom(16)}  # current per-connection nonce (AAD)

    def set_value(uuid: str, data: bytes) -> None:
        server.get_characteristic(uuid).value = data

    def push(text: str) -> None:
        set_value(STATUS_UUID, text.encode("utf-8"))
        server.update_value(SERVICE_UUID, STATUS_UUID)
        print(f"[provisioning] status -> {text}")

    async def handle_credentials(raw: bytes) -> None:
        challenge = state["challenge"]
        try:
            env = json.loads(bytes(raw))
            mode = env["mode"]
            blob = env["blob"]
        except Exception as exc:  # malformed envelope
            print(f"[provisioning] bad envelope: {exc!r}")
            push("failed:bad_payload")
            return

        # Decrypt the credentials with the right key for the mode.
        try:
            if mode == "reprovision":
                if provisioning_key is None:
                    push("failed:not_claimed")
                    return
                session_key = provisioning_key
            elif mode == "claim":
                phone_pub = bytes.fromhex(env["phonePub"])
                session_key = derive_session_key(box_priv, phone_pub)
            else:
                push("failed:bad_mode")
                return
            creds = open_credentials(session_key, blob, challenge)
            ssid, password = creds["ssid"], creds.get("password", "")
        except Exception as exc:  # wrong key / tampered / stale challenge
            print(f"[provisioning] decrypt failed: {exc!r}")
            push("failed:auth")
            return

        print(f"[provisioning] received SSID {ssid!r} (mode={mode}, password hidden)")
        push("credentials_received")
        push("connecting")
        try:
            await _nmcli_connect(ssid, password)
        except Exception as exc:
            push(f"failed:{_classify_failure(str(exc))}")
            return

        # First claim: mint the persistent key + hand it back encrypted so the
        # phone can re-provision this box later without another ECDH.
        if mode == "claim":
            minted = new_provisioning_key()
            set_value(KEYOUT_UUID, seal(session_key, minted, challenge).encode("utf-8"))
            save_provisioning_key(minted)
            print("[provisioning] minted + stored provisioning_key; KEYOUT ready")

        push("connected")
        done.set()

    def on_write(characteristic: BlessGATTCharacteristic, value, **kwargs) -> None:
        if characteristic.uuid.lower() == CREDS_UUID.lower():
            asyncio.run_coroutine_threadsafe(handle_credentials(bytes(value)), loop)

    def on_read(characteristic: BlessGATTCharacteristic, **kwargs):
        # Fresh challenge on every read → binds the next write to this session.
        if characteristic.uuid.lower() == CHALLENGE_UUID.lower():
            state["challenge"] = os.urandom(16)
            characteristic.value = state["challenge"].hex().encode("utf-8")
        return characteristic.value

    server.read_request_func = on_read
    server.write_request_func = on_write

    await server.add_new_service(SERVICE_UUID)

    read = GATTCharacteristicProperties.read
    write = GATTCharacteristicProperties.write
    notify = GATTCharacteristicProperties.notify
    readable = GATTAttributePermissions.readable
    writeable = GATTAttributePermissions.writeable

    device_info = json.dumps(
        {"device_id": device_id, "claimed": provisioning_key is not None, "slug": slug}
    ).encode("utf-8")
    await server.add_new_characteristic(SERVICE_UUID, DEVICE_INFO_UUID, read, device_info, readable)
    await server.add_new_characteristic(
        SERVICE_UUID, PUBKEY_UUID, read, box_pub.hex().encode("utf-8"), readable
    )
    await server.add_new_characteristic(
        SERVICE_UUID, CHALLENGE_UUID, read, state["challenge"].hex().encode("utf-8"), readable
    )
    await server.add_new_characteristic(SERVICE_UUID, CREDS_UUID, write, None, writeable)
    await server.add_new_characteristic(SERVICE_UUID, STATUS_UUID, read | notify, None, readable)
    await server.add_new_characteristic(SERVICE_UUID, KEYOUT_UUID, read, None, readable)

    await server.start()
    claimed = provisioning_key is not None
    print(
        f"[provisioning] advertising {SERVICE_UUID} as {server.name!r} "
        f"(claimed={claimed}); awaiting WiFi"
    )
    try:
        await done.wait()
        # Let the phone read KEYOUT (first claim) and the terminal status before
        # we tear down the BLE server (stopping it disconnects the phone).
        await asyncio.sleep(2.5)
    finally:
        try:
            await server.stop()
        except Exception:
            pass
    return True
