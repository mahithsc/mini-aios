#!/usr/bin/env bash
# AIOS box bootstrap — idempotent host config the box needs beyond the app.
# Run once on a fresh box (or after a reflash): sudo ./setup.sh
# Safe to re-run any time.
#
# Configures:
#   1. Bluetooth — disable BlueZ's `battery` + `deviceinfo` client plugins. When
#      an iPhone connects, BlueZ otherwise auto-reads the phone's Battery/Device-
#      Info services, which iOS gates behind pairing -> a spurious "would like to
#      pair" prompt during Wi-Fi setup. Disabling those plugins removes the probe.
#   2. aios-box systemd service — runs BLE provisioning when offline, then the
#      server. Enabled to start on boot.
#
# Optional (off by default): blacklist the Jetson vendor `rtk_btusb` driver so a
# USB BLE dongle (e.g. UB500 / RTL8761BU) enumerates on the mainline btusb stack
# instead of hanging at boot. Enable ONLY when using the dongle.

set -euo pipefail

# ---- tunables --------------------------------------------------------------
BOX_USER="mahithc"
BOX_HOME="/home/${BOX_USER}"
REPO_DIR="${BOX_HOME}/mini-aios"
VENV_PY="${REPO_DIR}/.venv/bin/python"
# 1 = force BLE provisioning on every boot even when online (testing: box always
# re-enters setup mode). 0 = normal (only provisions when actually offline).
FORCE_PROVISIONING=1
# 1 = blacklist rtk_btusb so a USB BLE dongle works. 0 = leave onboard radio as-is.
# NOTE: untested on this box; may affect BT at boot — do it with physical access.
ENABLE_DONGLE=0
# ---------------------------------------------------------------------------

if [[ $EUID -ne 0 ]]; then
  echo "Re-running with sudo..."
  exec sudo -E bash "$0" "$@"
fi

echo "==> [1/3] Bluetooth: disable battery + deviceinfo plugins (no iOS pair prompt)"
install -d /etc/systemd/system/bluetooth.service.d
# Named zz- so it applies AFTER NVIDIA's nv-bluetooth-service.conf drop-in
# (systemd merges drop-ins in filename order; last one wins).
cat > /etc/systemd/system/bluetooth.service.d/zz-noplugin.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/lib/bluetooth/bluetoothd -d --noplugin=audio,a2dp,avrcp,battery,deviceinfo
EOF

echo "==> [2/3] aios-box systemd service"
{
  echo "[Unit]"
  echo "Description=AIOS box (mini-aios)"
  echo "After=network-online.target NetworkManager.service bluetooth.service"
  echo "Wants=network-online.target"
  echo ""
  echo "[Service]"
  echo "Type=simple"
  echo "User=root"
  echo "WorkingDirectory=${REPO_DIR}"
  echo "Environment=HOME=${BOX_HOME}"
  echo "Environment=PYTHONUNBUFFERED=1"
  [[ "${FORCE_PROVISIONING}" == "1" ]] && echo "Environment=AIOS_FORCE_PROVISIONING=1"
  echo "ExecStart=${VENV_PY} main.py"
  echo "Restart=always"
  echo "RestartSec=3"
  echo ""
  echo "[Install]"
  echo "WantedBy=multi-user.target"
} > /etc/systemd/system/aios-box.service

if [[ "${ENABLE_DONGLE}" == "1" ]]; then
  echo "==> [opt] Blacklisting rtk_btusb for USB BLE dongle"
  echo 'blacklist rtk_btusb' > /etc/modprobe.d/blacklist-rtk-btusb.conf
  update-initramfs -u || true
fi

echo "==> [3/3] Reload + (re)start services"
systemctl daemon-reload
systemctl enable aios-box.service >/dev/null 2>&1 || true
systemctl restart bluetooth
sleep 2
systemctl restart aios-box.service

set +e
echo
echo "Done. Verify:"
BT_PID="$(pgrep -x bluetoothd)"
if [[ -n "${BT_PID}" ]]; then
  echo "  bluetoothd: $(tr '\0' ' ' < /proc/${BT_PID}/cmdline)"
else
  echo "  bluetoothd: (not running yet)"
fi
echo "  bluetooth:  $(systemctl is-active bluetooth)"
echo "  aios-box:   $(systemctl is-active aios-box.service)"
