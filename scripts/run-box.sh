#!/usr/bin/env bash
#
# run-box.sh — REGULAR provisioning (re-set-up an already-known box).
#
# Puts the box into BLE WiFi setup (advertising) while KEEPING its identity, so a
# phone that has claimed it before re-provisions it (mode=reprovision, reuses the
# stored provisioning key — e.g. moving the box to a new WiFi). For a brand-new /
# clean claim instead, use run-box-fresh.sh.
#
# Run it on the box — no env/config needed:
#     ./scripts/run-box.sh
#
# It auto-elevates (nmcli WiFi join needs root, HOME preserved so .env/workspace
# resolve), stops the persistent aios-box service to free the BLE adapter + :8765,
# and runs main.py in the foreground with live logs. Ctrl+C to stop;
# `sudo systemctl start aios-box` to resume the persistent service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
REPO="$(dirname "$SCRIPT_DIR")"
VENV="$REPO/.venv/bin/python"

# nmcli's WiFi join needs root; re-exec under sudo, preserving HOME so the app's
# workspace/.env resolve to the real user (not /root).
if [ "$(id -u)" -ne 0 ]; then
  echo "[run-box] elevating for nmcli (HOME=$HOME preserved)"
  exec sudo env "HOME=$HOME" "$SELF" "$@"
fi

[ -x "$VENV" ] || { echo "[run-box] no venv python at $VENV — create it (uv sync) first"; exit 1; }

echo "[run-box] stopping persistent service (if running) to free BLE + :8765"
systemctl stop aios-box 2>/dev/null || true

echo "[run-box] REGULAR provisioning — advertising BLE for setup, keeping identity"
echo "[run-box] pair from the app; Ctrl+C to stop  (resume service: sudo systemctl start aios-box)"
cd "$REPO"
exec env PYTHONUNBUFFERED=1 AIOS_FORCE_PROVISIONING=1 "$VENV" main.py
