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

The GATT server uses `bluez-peripheral` (talks to BlueZ's GATT D-Bus API directly)
rather than `bless`: bless couldn't reliably serve a second/sequential connection
(the phone reconnects to write after reading the WiFi list), which broke the flow.

GATT (all plain; security is in the payload):
    Service 7A71E000-1111-2222-3333-123456789ABC
      CREDS       7A71E001 write   <- {"mode","phonePub"?,"blob"} (JSON)
      STATUS      7A71E002 notify  -> credentials_received|connecting|connected|failed:<reason>
      PUBKEY      7A71E003 read    -> box ephemeral X25519 pubkey (hex) [first claim]
      CHALLENGE   7A71E004 read    -> fresh 16-byte nonce (hex), regenerated per read
      DEVICE_INFO 7A71E005 read    -> {"device_id","claimed":bool,"slug"}
      KEYOUT      7A71E006 read    -> provisioning_key sealed under session key (base64) [first claim]
      LANIP       7A71E007 read    -> the box's LAN IP once joined (for direct pairing)
      NETWORKS    7A71E008 read    -> [[ssid, signal, secure01], ...] the box can see
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess

from bluez_peripheral.gatt.service import Service
from bluez_peripheral.gatt.characteristic import (
    characteristic,
    CharacteristicFlags as CharFlags,
)
from bluez_peripheral.advert import Advertisement
from bluez_peripheral.util import Adapter, get_message_bus

from aios_core.db import get_device_link, get_provisioning_key, save_provisioning_key
from server.discovery import _primary_lan_ip
from server.provisioning_crypto import (
    derive_session_key,
    generate_ephemeral,
    new_provisioning_key,
    open_credentials,
    seal,
)

# The Jetson's onboard Realtek radio can't hold LE connections reliably; a USB
# BLE dongle can. Prefer the dongle by advertising on whichever adapter isn't the
# onboard one (identified by MAC), falling back to the default if it's the only one.
ONBOARD_BT_MAC = "9c:c7:d3:f6:b8:84"

SERVICE_UUID = "7A71E000-1111-2222-3333-123456789ABC"
CREDS_UUID = "7A71E001-1111-2222-3333-123456789ABC"
STATUS_UUID = "7A71E002-1111-2222-3333-123456789ABC"
PUBKEY_UUID = "7A71E003-1111-2222-3333-123456789ABC"
CHALLENGE_UUID = "7A71E004-1111-2222-3333-123456789ABC"
DEVICE_INFO_UUID = "7A71E005-1111-2222-3333-123456789ABC"
KEYOUT_UUID = "7A71E006-1111-2222-3333-123456789ABC"
LANIP_UUID = "7A71E007-1111-2222-3333-123456789ABC"
NETWORKS_UUID = "7A71E008-1111-2222-3333-123456789ABC"


async def _select_adapter(bus) -> Adapter:
    """Return the BLE adapter to advertise on, preferring an external dongle.

    Picks the first adapter whose MAC isn't the onboard radio; falls back to the
    default adapter when the onboard radio is the only one.
    """
    try:
        adapters = await Adapter.get_all(bus)
    except Exception as exc:  # noqa: BLE001 - best-effort
        print(f"[provisioning] adapter lookup failed: {exc!r}")
        return await Adapter.get_first(bus)
    for adapter in adapters:
        try:
            addr = (await adapter.get_address()).lower()
        except Exception:
            continue
        if addr and addr != ONBOARD_BT_MAC:
            print(f"[provisioning] using BLE adapter {addr} — external dongle")
            return adapter
    print("[provisioning] no external BLE adapter; using onboard radio (may be flaky)")
    return await Adapter.get_first(bus)


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


def _compact_networks(nets: list[dict]) -> bytes:
    """[[ssid, signal, secure01], ...] trimmed to fit a single BLE read (~185 B)."""
    compact = [[n["ssid"], n["signal"], 1 if n["secure"] else 0] for n in nets]
    while len(json.dumps(compact)) > 180 and len(compact) > 1:
        compact.pop()
    return json.dumps(compact).encode("utf-8")


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


def _run(args: list[str]) -> None:
    try:
        subprocess.run(args, capture_output=True, timeout=5)
    except Exception as exc:  # noqa: BLE001 - best-effort
        print(f"[provisioning] {args[0]} failed: {exc!r}")


def _disable_bonding(adapter_mac: str) -> None:
    """Make provisioning pairing-free (our security is app-layer, not OS bonding).

    Sets the adapter non-bondable/non-pairable AND wipes any stored bond. This
    prevents the failure where iOS "Forget this device" deletes its half of the
    bond but leaves the box's half — a one-sided bond makes iOS terminate every
    reconnect (HCI reason 0x13 Remote User Terminated). Best-effort shell-out.
    """
    try:
        out = subprocess.run(["hciconfig"], capture_output=True, text=True, timeout=5).stdout
    except Exception as exc:  # noqa: BLE001
        print(f"[provisioning] hciconfig failed in _disable_bonding: {exc!r}")
        return
    hci = cur = None
    for line in out.splitlines():
        if line[:3] == "hci" and ":" in line[:6]:
            cur = line.split(":", 1)[0]
        elif "BD Address:" in line and cur:
            addr = line.split("BD Address:", 1)[1].strip().split()[0].lower()
            if addr == adapter_mac.lower():
                hci = cur
                break
    if not hci:
        return
    idx = hci[3:]
    _run(["btmgmt", "--index", idx, "bondable", "off"])
    _run(["busctl", "set-property", "org.bluez", f"/org/bluez/{hci}",
          "org.bluez.Adapter1", "Pairable", "b", "false"])
    ctrl_dir = f"/var/lib/bluetooth/{adapter_mac.upper()}"
    try:
        for entry in os.listdir(ctrl_dir):
            if entry.count(":") == 5 and os.path.isdir(os.path.join(ctrl_dir, entry)):
                _run(["btmgmt", "--index", idx, "unpair", entry])
                print(f"[provisioning] cleared stale bond {entry}")
    except FileNotFoundError:
        pass


class ProvisioningService(Service):
    """The GATT service the phone talks to. Reads serve device info / WiFi list /
    crypto material; the single write (CREDS) drives the encrypted handoff."""

    def __init__(self, device_id: str, provisioning_key, slug, networks: bytes, done: asyncio.Event):
        super().__init__(SERVICE_UUID, True)
        self._device_id = device_id
        self._provisioning_key = provisioning_key  # bytes or None
        self._done = done
        self._box_priv, self._box_pub = generate_ephemeral()
        self._challenge = os.urandom(16)
        self._device_info = json.dumps(
            {"device_id": device_id, "claimed": provisioning_key is not None, "slug": slug}
        ).encode("utf-8")
        self._networks = networks
        self._keyout = b""
        self._lanip = b""
        self._status = b""

    # --- reads -------------------------------------------------------------
    @characteristic(DEVICE_INFO_UUID, CharFlags.READ)
    def device_info(self, _options):
        return self._device_info

    @characteristic(NETWORKS_UUID, CharFlags.READ)
    def networks(self, _options):
        return self._networks

    @characteristic(PUBKEY_UUID, CharFlags.READ)
    def pubkey(self, _options):
        return self._box_pub.hex().encode("utf-8")

    @characteristic(CHALLENGE_UUID, CharFlags.READ)
    def challenge(self, _options):
        self._challenge = os.urandom(16)  # fresh per-connection nonce (GCM AAD)
        return self._challenge.hex().encode("utf-8")

    @characteristic(KEYOUT_UUID, CharFlags.READ)
    def keyout(self, _options):
        return self._keyout

    @characteristic(LANIP_UUID, CharFlags.READ)
    def lanip(self, _options):
        return self._lanip

    @characteristic(STATUS_UUID, CharFlags.READ | CharFlags.NOTIFY)
    def status(self, _options):
        return self._status

    # --- write (drives the handoff) ---------------------------------------
    @characteristic(CREDS_UUID, CharFlags.WRITE)
    def creds(self, _options):
        return b""

    @creds.setter
    def creds(self, value, _options):
        asyncio.ensure_future(self._handle_credentials(bytes(value)))

    # --- helpers -----------------------------------------------------------
    def _push(self, text: str) -> None:
        self._status = text.encode("utf-8")
        try:
            self.status.changed(self._status)
        except Exception as exc:  # noqa: BLE001 - notify is best-effort
            print(f"[provisioning] status notify failed: {exc!r}")
        print(f"[provisioning] status -> {text}")

    async def _handle_credentials(self, raw: bytes) -> None:
        challenge = self._challenge
        try:
            env = json.loads(bytes(raw))
            mode = env["mode"]
            blob = env["blob"]
        except Exception as exc:
            print(f"[provisioning] bad envelope: {exc!r}")
            self._push("failed:bad_payload")
            return

        try:
            if mode == "reprovision":
                if self._provisioning_key is None:
                    self._push("failed:not_claimed")
                    return
                session_key = self._provisioning_key
            elif mode == "claim":
                session_key = derive_session_key(self._box_priv, bytes.fromhex(env["phonePub"]))
            else:
                self._push("failed:bad_mode")
                return
            creds = open_credentials(session_key, blob, challenge)
            ssid, password = creds["ssid"], creds.get("password", "")
        except Exception as exc:  # wrong key / tampered / stale challenge
            print(f"[provisioning] decrypt failed: {exc!r}")
            self._push("failed:auth")
            return

        print(f"[provisioning] received SSID {ssid!r} (mode={mode}, encrypted)")
        self._push("credentials_received")
        self._push("connecting")
        try:
            await _nmcli_connect(ssid, password)
        except Exception as exc:
            self._push(f"failed:{_classify_failure(str(exc))}")
            return

        # Report our new LAN IP so the phone can pair directly to us (no mDNS).
        self._lanip = _primary_lan_ip().encode("utf-8")

        # First claim: mint the persistent key + hand it back (encrypted) so the
        # phone can re-provision this box later without another ECDH.
        if mode == "claim":
            minted = new_provisioning_key()
            self._keyout = seal(session_key, minted, challenge).encode("utf-8")
            save_provisioning_key(minted)
            print("[provisioning] minted + stored provisioning_key; KEYOUT ready")

        self._push("connected")
        self._done.set()


async def run_provisioning(device_id: str, name: str | None = None) -> bool:
    """Advertise over BLE and block until handed (encrypted) WiFi credentials and
    joined. Returns True once online. Failed attempts keep advertising for retry.
    """
    done = asyncio.Event()
    provisioning_key = get_provisioning_key()  # bytes or None
    link = get_device_link()
    slug = link.get("slug") if link else None
    networks = _compact_networks(await _scan_networks())

    bus = await get_message_bus()
    adapter = await _select_adapter(bus)

    # Pairing-free: no OS bond ever forms (we encrypt at the app layer), so an iOS
    # "Forget this device" can't leave a one-sided bond that kills every reconnect.
    try:
        _disable_bonding(await adapter.get_address())
    except Exception as exc:  # noqa: BLE001 - non-fatal
        print(f"[provisioning] disable_bonding skipped: {exc!r}")

    service = ProvisioningService(device_id, provisioning_key, slug, networks, done)
    await service.register(bus, adapter=adapter)

    # No pairing agent on purpose — without one, BlueZ rejects any pairing attempt,
    # keeping the box pairing-free. iOS connects plain (our characteristics carry no
    # encryption flags), which is all the provisioning handshake needs.

    advert = Advertisement(
        name or f"aios-{device_id[:8]}", [SERVICE_UUID], appearance=0, timeout=0
    )
    await advert.register(bus, adapter=adapter)

    claimed = provisioning_key is not None
    print(
        f"[provisioning] advertising {SERVICE_UUID} as {name or f'aios-{device_id[:8]}'!r} "
        f"(claimed={claimed}); awaiting WiFi"
    )
    try:
        await done.wait()
        # Let the phone read KEYOUT (first claim) + the terminal status before teardown.
        await asyncio.sleep(2.5)
    finally:
        for cleanup in (
            lambda: advert.unregister(),
            lambda: service.unregister(),
        ):
            try:
                res = cleanup()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass
        try:
            bus.disconnect()
        except Exception:
            pass
    return True
