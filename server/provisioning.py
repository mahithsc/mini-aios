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
import time

import httpx

from bluez_peripheral.gatt.service import Service
from bluez_peripheral.gatt.characteristic import (
    characteristic,
    CharacteristicFlags as CharFlags,
)
from bluez_peripheral.advert import Advertisement
from bluez_peripheral.agent import NoIoAgent
from bluez_peripheral.util import Adapter, get_message_bus

from aios_core.db import get_device_link, get_provisioning_key, save_provisioning_key
from server.discovery import _primary_lan_ip
from server.pairing import cloud_url, complete_pairing
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


def _wipe_bonds() -> None:
    """Delete ALL stored BLE bonds so every provisioning session pairs FRESH.

    iOS *wants* to bond and only succeeds when it can pair fresh — the failure
    mode is a one-sided/stale bond (e.g. iOS "Forget this device" deletes its
    half but leaves the box's), which makes iOS terminate every reconnect (HCI
    reason 0x13). We stay bondable + keep the agent so pairing "just works"; this
    just guarantees the box starts each setup with no leftover bond.

    Stop bluetoothd → delete the on-disk bonds → restart. This is the ONLY
    reliable removal (RemoveDevice misses disk-only bonds; `btmgmt unpair` leaves
    the files). Safe here: run at provisioning start, before we open the BLE bus
    and before anything else uses Bluetooth. Best-effort.
    """
    import re
    import shutil

    mac_re = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
    base = "/var/lib/bluetooth"
    try:
        subprocess.run(["systemctl", "stop", "bluetooth"], capture_output=True, timeout=10)
        time.sleep(2)
        wiped = 0
        for ctrl in os.listdir(base):  # controller (adapter) dirs
            ctrl_dir = os.path.join(base, ctrl)
            if not os.path.isdir(ctrl_dir):
                continue
            for dev in os.listdir(ctrl_dir):  # bonded device dirs
                dev_dir = os.path.join(ctrl_dir, dev)
                if mac_re.match(dev) and os.path.isdir(dev_dir):
                    shutil.rmtree(dev_dir, ignore_errors=True)
                    wiped += 1
        subprocess.run(["systemctl", "start", "bluetooth"], capture_output=True, timeout=10)
        time.sleep(3)
        print(f"[provisioning] wiped {wiped} stored BLE bond(s) — fresh pairing each setup")
    except Exception as exc:  # noqa: BLE001
        print(f"[provisioning] bond wipe skipped: {exc!r}")


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
        # Set when the phone reads a populated KEYOUT — the handoff waits on this
        # before joining WiFi so the key is delivered while BLE is still healthy.
        self._keyout_read = asyncio.Event()
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
        # A read of a populated KEYOUT is the phone's ACK that it has the key;
        # _handle_credentials waits on this before joining WiFi.
        if self._keyout:
            self._keyout_read.set()
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
            # Off-LAN pairing: the phone may include the cloud pairing code in the
            # (encrypted) creds blob so WE redeem it ourselves after joining WiFi and
            # relay the result — no LAN round-trip to the phone required. Absent for
            # the legacy LAN-only flow (phone POSTs /pair itself).
            pairing_code = creds.get("pairing_code")
        except Exception as exc:  # wrong key / tampered / stale challenge
            print(f"[provisioning] decrypt failed: {exc!r}")
            self._push("failed:auth")
            return

        print(f"[provisioning] received SSID {ssid!r} (mode={mode}, encrypted)")
        self._push("credentials_received")

        # First claim: mint the persistent key + publish it on KEYOUT NOW, BEFORE
        # joining WiFi. Joining drops the BLE link on the onboard combo radio
        # (WiFi/BT coexistence); handing the key back AFTER the join (as before)
        # meant the phone usually couldn't read it and could never re-provision
        # later (it dead-ended at "set up with another device"). Signal key_ready,
        # wait for the phone to read it, then join.
        # The persistent per-box key the phone holds (from KEYOUT on first claim, or
        # stored from a prior claim on reprovision) — used to seal the pairing result
        # we relay back. Distinct from `session_key`, which on first claim is the
        # ephemeral ECDH key the phone can't reuse.
        prov_key = self._provisioning_key
        if mode == "claim":
            minted = new_provisioning_key()
            self._keyout = seal(session_key, minted, challenge).encode("utf-8")
            save_provisioning_key(minted)
            prov_key = minted
            print("[provisioning] minted + stored provisioning_key; KEYOUT ready")
            self._push("key_ready")
            try:
                await asyncio.wait_for(self._keyout_read.wait(), timeout=8.0)
                print("[provisioning] phone read KEYOUT — joining WiFi")
            except asyncio.TimeoutError:
                print("[provisioning] KEYOUT not read in time — joining WiFi anyway")

        self._push("connecting")
        try:
            await _nmcli_connect(ssid, password)
        except Exception as exc:
            self._push(f"failed:{_classify_failure(str(exc))}")
            return

        # Report our new LAN IP so the phone can pair directly to us (no mDNS).
        self._lanip = _primary_lan_ip().encode("utf-8")

        self._push("connected")

        # Off-LAN pairing: if the phone handed us a pairing code, redeem it and
        # relay the sealed result via the cloud before we finish (the box is online
        # now, so this works regardless of the phone's network). Best-effort — the
        # box stays online either way and the phone can retry.
        if pairing_code:
            await self._relay_pairing_result(pairing_code, prov_key)

        self._done.set()

    async def _relay_pairing_result(self, pairing_code: str, prov_key: bytes) -> None:
        """Off-LAN pairing: redeem the phone's pairing code ourselves (we're online
        now), then relay the SEALED {local_token, hostname, ...} to the cloud for the
        phone to fetch. The phone can't reach us directly (different network, and no
        public tunnel exists until this very claim completes), so this is how it
        learns the local_token. Sealed under the persistent provisioning_key the
        phone already holds — the cloud only ever relays ciphertext."""
        try:
            result = await complete_pairing(pairing_code)
        except Exception as exc:  # noqa: BLE001 - box stays online; phone can retry
            print(f"[provisioning] self-complete pairing failed: {exc!r}")
            self._push("failed:pairing")
            return

        link = get_device_link()
        device_token = link.get("device_token") if link else None
        if not device_token:
            print("[provisioning] no device_token after pairing; cannot relay result")
            return

        payload = json.dumps(
            {
                "local_token": result["local_token"],
                "slug": result.get("slug"),
                "hostname": result.get("hostname"),
                "device_id": self._device_id,
            }
        ).encode("utf-8")
        # AAD = device_id (there's no live BLE challenge at this point); the phone
        # opens with the same provisioning_key + AAD.
        sealed = seal(prov_key, payload, self._device_id.encode("utf-8"))

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{cloud_url()}/device/pairing-result",
                    headers={"Authorization": f"Bearer {device_token}"},
                    json={"sealed": sealed},
                )
        except httpx.HTTPError as exc:
            print(f"[provisioning] relay pairing-result failed: {exc!r}")
            self._push("failed:relay")
            return
        if resp.status_code not in (200, 204):
            print(f"[provisioning] relay pairing-result HTTP {resp.status_code}: {resp.text}")
            self._push("failed:relay")
            return
        print("[provisioning] pairing result relayed to cloud")
        self._push("paired")


async def run_provisioning(device_id: str, name: str | None = None) -> bool:
    """Advertise over BLE and block until handed (encrypted) WiFi credentials and
    joined. Returns True once online. Failed attempts keep advertising for retry.
    """
    done = asyncio.Event()
    provisioning_key = get_provisioning_key()  # bytes or None
    link = get_device_link()
    slug = link.get("slug") if link else None
    networks = _compact_networks(await _scan_networks())

    # Wipe any stale BLE bond before the radio comes up, so iOS pairs FRESH (a
    # stale/one-sided bond makes iOS terminate every reconnect — HCI reason 0x13).
    _wipe_bonds()

    bus = await get_message_bus()
    adapter = await _select_adapter(bus)

    service = ProvisioningService(device_id, provisioning_key, slug, networks, done)
    await service.register(bus, adapter=adapter)

    # No-IO agent so pairing "just works" without a prompt when iOS bonds fresh.
    try:
        agent = NoIoAgent()
        await agent.register(bus)
    except Exception as exc:  # noqa: BLE001 - non-fatal
        print(f"[provisioning] agent register skipped: {exc!r}")

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
