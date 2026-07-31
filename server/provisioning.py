"""BLE WiFi provisioning for a fresh/off-network box ("true boot"), encrypted + remembered.

The BLE transport is PLAIN (no OS bonding), but the WiFi credentials are encrypted
app-layer, and the box + phone remember each other so a moved box can be
re-provisioned without a fresh handshake:

- **First claim** (box has no stored key): the box publishes an ephemeral X25519
  pubkey (PUBKEY) + a per-connection nonce (CHALLENGE). The phone does ECDH →
  one-time AES-256-GCM key, encrypts {ssid,password}, box decrypts + joins. Then
  the box MINTS a persistent `provisioning_key`, stores it, and returns it to the
  phone encrypted under the session key (KEYOUT). Both persist it.
- **Re-provision** (box already claimed, e.g. moved to a new network): the phone
  recognizes the box by its `device_id` (DEVICE_INFO), loads the stored
  `provisioning_key`, and encrypts the new creds with it directly — no ECDH. Only
  the phone holding that key can re-provision the box.

No key is stored in the Bluetooth stack and no bonding happens — the "memory" is
just the shared `provisioning_key` (box DB + phone keychain), keyed by device_id.
Crypto lives in provisioning_crypto.py (interoperable with the phone's @noble impl).

GATT (all plain; security is in the payload):
    Service 7A71E000-1111-2222-3333-123456789ABC
      CREDS       7A71E001 write   <- {"mode","phonePub"?,"blob"} (JSON)
      STATUS      7A71E002 notify  -> credentials_received|connecting|connected|failed:<reason>
      PUBKEY      7A71E003 read    -> box ephemeral X25519 pubkey (hex) [first claim]
      CHALLENGE   7A71E004 read    -> fresh 16-byte nonce (hex), regenerated per read
      DEVICE_INFO 7A71E005 read    -> {"device_id","claimed":bool,"slug"}
      KEYOUT      7A71E006 read    -> provisioning_key sealed under session key (base64) [first claim]
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
from server.discovery import _primary_lan_ip
from server.provisioning_crypto import (
    derive_session_key,
    generate_ephemeral,
    new_provisioning_key,
    open_credentials,
    seal,
)

SERVICE_UUID = "7A71E000-1111-2222-3333-123456789ABC"
CREDS_UUID = "7A71E001-1111-2222-3333-123456789ABC"
STATUS_UUID = "7A71E002-1111-2222-3333-123456789ABC"
PUBKEY_UUID = "7A71E003-1111-2222-3333-123456789ABC"
CHALLENGE_UUID = "7A71E004-1111-2222-3333-123456789ABC"
DEVICE_INFO_UUID = "7A71E005-1111-2222-3333-123456789ABC"
KEYOUT_UUID = "7A71E006-1111-2222-3333-123456789ABC"
LANIP_UUID = "7A71E007-1111-2222-3333-123456789ABC"
NETWORKS_UUID = "7A71E008-1111-2222-3333-123456789ABC"


async def _scan_networks() -> list[dict]:
    """Scan visible WiFi networks so the phone can show a picker.

    Returns [{ssid, signal, secure}], de-duplicated by SSID (strongest kept),
    strongest first. Best-effort — an empty list on failure just means the user
    types the SSID manually.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _ = await proc.communicate()
    except Exception as exc:  # noqa: BLE001 - best-effort
        print(f"[provisioning] wifi scan failed: {exc}")
        return []

    nets: dict[str, dict] = {}
    for line in (out or b"").decode(errors="replace").splitlines():
        # Terse output: SSID:SIGNAL:SECURITY, with ':' inside fields escaped as '\:'.
        parts = line.rsplit(":", 2)
        if len(parts) != 3:
            continue
        ssid = parts[0].replace("\\:", ":").replace("\\\\", "\\").strip()
        if not ssid:
            continue
        signal = int(parts[1]) if parts[1].isdigit() else 0
        secure = parts[2].strip() not in ("", "--")
        if ssid not in nets or signal > nets[ssid]["signal"]:
            nets[ssid] = {"ssid": ssid, "signal": signal, "secure": secure}
    return sorted(nets.values(), key=lambda n: -n["signal"])


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
    """Advertise over BLE and block until handed (encrypted) WiFi credentials and
    joined. Returns True once online. Failed attempts keep advertising for retry.
    """
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    server = BlessServer(name=name or f"aios-{device_id[:8]}", loop=loop)

    provisioning_key = get_provisioning_key()  # bytes or None
    link = get_device_link()
    slug = link.get("slug") if link else None
    box_priv, box_pub = generate_ephemeral()  # ephemeral X25519 for first-claim ECDH
    state = {"challenge": os.urandom(16)}  # per-connection nonce (GCM AAD)

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
        except Exception as exc:
            print(f"[provisioning] bad envelope: {exc!r}")
            push("failed:bad_payload")
            return

        try:
            if mode == "reprovision":
                if provisioning_key is None:
                    push("failed:not_claimed")
                    return
                session_key = provisioning_key
            elif mode == "claim":
                session_key = derive_session_key(box_priv, bytes.fromhex(env["phonePub"]))
            else:
                push("failed:bad_mode")
                return
            creds = open_credentials(session_key, blob, challenge)
            ssid, password = creds["ssid"], creds.get("password", "")
        except Exception as exc:  # wrong key / tampered / stale challenge
            print(f"[provisioning] decrypt failed: {exc!r}")
            push("failed:auth")
            return

        print(f"[provisioning] received SSID {ssid!r} (mode={mode}, encrypted)")
        push("credentials_received")
        push("connecting")
        try:
            await _nmcli_connect(ssid, password)
        except Exception as exc:
            push(f"failed:{_classify_failure(str(exc))}")
            return

        # Report our new LAN IP so the phone can pair directly to us (no mDNS).
        set_value(LANIP_UUID, _primary_lan_ip().encode("utf-8"))

        # First claim: mint the persistent key + hand it back (encrypted) so the
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

    device_info = json.dumps(
        {"device_id": device_id, "claimed": provisioning_key is not None, "slug": slug}
    ).encode("utf-8")
    networks = json.dumps(await _scan_networks()).encode("utf-8")

    await server.add_new_service(SERVICE_UUID)
    await server.add_new_characteristic(SERVICE_UUID, CREDS_UUID, write, None, writeable)
    await server.add_new_characteristic(SERVICE_UUID, STATUS_UUID, read | notify, None, readable)
    await server.add_new_characteristic(
        SERVICE_UUID, PUBKEY_UUID, read, box_pub.hex().encode("utf-8"), readable
    )
    await server.add_new_characteristic(
        SERVICE_UUID, CHALLENGE_UUID, read, state["challenge"].hex().encode("utf-8"), readable
    )
    await server.add_new_characteristic(SERVICE_UUID, DEVICE_INFO_UUID, read, device_info, readable)
    await server.add_new_characteristic(SERVICE_UUID, KEYOUT_UUID, read, None, readable)
    await server.add_new_characteristic(SERVICE_UUID, LANIP_UUID, read, None, readable)
    await server.add_new_characteristic(SERVICE_UUID, NETWORKS_UUID, read, networks, readable)

    await server.start()
    claimed = provisioning_key is not None
    print(
        f"[provisioning] advertising {SERVICE_UUID} as {server.name!r} "
        f"(claimed={claimed}); awaiting WiFi"
    )
    try:
        await done.wait()
        # Let the phone read KEYOUT (first claim) + the terminal status before we
        # tear down the BLE server (stopping it disconnects the phone).
        await asyncio.sleep(2.5)
    finally:
        try:
            await server.stop()
        except Exception:
            pass
    return True
