"""BLE WiFi provisioning for a fresh/off-network box ("true boot"), encrypted.

On boot with no network, `main.py` runs this so a phone can hand the box WiFi
credentials over Bluetooth. The BLE transport is PLAIN (no OS bonding), but the
WiFi credentials are encrypted at the app layer so the password never crosses the
air in the clear:

- The box publishes an ephemeral X25519 public key (PUBKEY) and a fresh nonce
  (CHALLENGE) per read.
- The phone does ECDH against that pubkey → a one-time AES-256-GCM key, encrypts
  {ssid,password} (with the challenge as GCM AAD → anti-replay), and writes the
  envelope {phonePub, blob} to CREDS.
- The box derives the same key, decrypts, and joins with `nmcli`.

No key is stored and no bonding happens — it's a per-connection encrypted handoff.
Crypto lives in provisioning_crypto.py (interoperable with the phone's @noble impl).

GATT (all plain; security is in the payload):
    Service 7A71E000-1111-2222-3333-123456789ABC
      CREDS     7A71E001 write   <- {"phonePub":"<hex>","blob":"<base64 sealed creds>"}
      STATUS    7A71E002 notify  -> credentials_received|connecting|connected|failed:<reason>
      PUBKEY    7A71E003 read    -> box ephemeral X25519 pubkey (hex)
      CHALLENGE 7A71E004 read    -> fresh 16-byte nonce (hex), regenerated per read
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

from server.provisioning_crypto import (
    derive_session_key,
    generate_ephemeral,
    open_credentials,
)

SERVICE_UUID = "7A71E000-1111-2222-3333-123456789ABC"
CREDS_UUID = "7A71E001-1111-2222-3333-123456789ABC"
STATUS_UUID = "7A71E002-1111-2222-3333-123456789ABC"
PUBKEY_UUID = "7A71E003-1111-2222-3333-123456789ABC"
CHALLENGE_UUID = "7A71E004-1111-2222-3333-123456789ABC"


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
    """Advertise over BLE and block until the box is handed (encrypted) WiFi
    credentials and joins the network. Returns True once online. Failed attempts
    keep the server advertising so the user can retry.
    """
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    server = BlessServer(name=name or f"aios-{device_id[:8]}", loop=loop)

    box_priv, box_pub = generate_ephemeral()  # ephemeral X25519 for ECDH
    state = {"challenge": os.urandom(16)}  # per-connection nonce (GCM AAD)

    def push(text: str) -> None:
        char = server.get_characteristic(STATUS_UUID)
        char.value = text.encode("utf-8")
        server.update_value(SERVICE_UUID, STATUS_UUID)
        print(f"[provisioning] status -> {text}")

    async def handle_credentials(raw: bytes) -> None:
        challenge = state["challenge"]
        try:
            env = json.loads(bytes(raw))
            phone_pub = bytes.fromhex(env["phonePub"])
            session_key = derive_session_key(box_priv, phone_pub)
            creds = open_credentials(session_key, env["blob"], challenge)
            ssid, password = creds["ssid"], creds.get("password", "")
        except Exception as exc:  # malformed / undecryptable / stale challenge
            print(f"[provisioning] could not read credentials: {exc!r}")
            push("failed:auth")
            return

        print(f"[provisioning] received SSID {ssid!r} (encrypted; password hidden)")
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
        # Fresh challenge on every read → binds the next write to this session.
        if characteristic.uuid.lower() == CHALLENGE_UUID.lower():
            state["challenge"] = os.urandom(16)
            characteristic.value = state["challenge"].hex().encode("utf-8")
        return characteristic.value

    server.read_request_func = on_read
    server.write_request_func = on_write

    read = GATTCharacteristicProperties.read
    write = GATTCharacteristicProperties.write
    notify = GATTCharacteristicProperties.notify
    readable = GATTAttributePermissions.readable
    writeable = GATTAttributePermissions.writeable

    await server.add_new_service(SERVICE_UUID)
    await server.add_new_characteristic(SERVICE_UUID, CREDS_UUID, write, None, writeable)
    await server.add_new_characteristic(SERVICE_UUID, STATUS_UUID, read | notify, None, readable)
    await server.add_new_characteristic(
        SERVICE_UUID, PUBKEY_UUID, read, box_pub.hex().encode("utf-8"), readable
    )
    await server.add_new_characteristic(
        SERVICE_UUID, CHALLENGE_UUID, read, state["challenge"].hex().encode("utf-8"), readable
    )

    await server.start()
    print(f"[provisioning] advertising {SERVICE_UUID} as {server.name!r} (encrypted); awaiting WiFi")
    try:
        await done.wait()
        # Let the phone receive the terminal "connected" status before we tear
        # down the BLE server (stopping it disconnects the phone).
        await asyncio.sleep(1.5)
    finally:
        try:
            await server.stop()
        except Exception:
            pass
    return True
