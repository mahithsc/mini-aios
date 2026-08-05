#!/usr/bin/env bash
#
# run-box-fresh.sh — FRESH provisioning (simulate a brand-new box).
#
# Wipes the box's identity — unpairs it (device_link) and drops the remembered
# provisioning key (provisioning_secret) — so the next BLE session is a first-time
# CLAIM (mode=claim, fresh ECDH key), pairable from any phone. Then advertises for
# setup. Use this for a clean end-to-end test; use run-box.sh to keep the identity.
#
# Run it on the box — no env/config needed:
#     ./scripts/run-box-fresh.sh
#
# Auto-elevates (nmcli needs root, HOME preserved), stops the persistent service,
# wipes identity, then runs main.py in the foreground with live logs. Ctrl+C to
# stop; `sudo systemctl start aios-box` to resume the persistent service.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$SCRIPT_DIR/$(basename "${BASH_SOURCE[0]}")"
REPO="$(dirname "$SCRIPT_DIR")"
VENV="$REPO/.venv/bin/python"

if [ "$(id -u)" -ne 0 ]; then
  echo "[run-box-fresh] elevating for nmcli (HOME=$HOME preserved)"
  exec sudo env "HOME=$HOME" "$SELF" "$@"
fi

[ -x "$VENV" ] || { echo "[run-box-fresh] no venv python at $VENV — create it (uv sync) first"; exit 1; }

echo "[run-box-fresh] stopping persistent service (if running)"
systemctl stop aios-box 2>/dev/null || true

echo "[run-box-fresh] wiping identity (device_link + provisioning_secret)"
cd "$REPO"
"$VENV" - <<'PY'
import sqlite3
from aios_core.db import DB_PATH
con = sqlite3.connect(DB_PATH)
for table in ("device_link", "provisioning_secret"):
    try:
        n = con.execute(f"DELETE FROM {table}").rowcount
        print(f"  cleared {table} ({n} row(s))")
    except sqlite3.OperationalError as exc:
        print(f"  {table}: {exc} (table absent — already fresh)")
con.commit()
con.close()
print("  identity wiped in", DB_PATH)
PY

echo "[run-box-fresh] FRESH provisioning — advertising BLE for a first-time claim"
echo "[run-box-fresh] pair from the app; Ctrl+C to stop  (resume service: sudo systemctl start aios-box)"
exec env PYTHONUNBUFFERED=1 AIOS_FORCE_PROVISIONING=1 "$VENV" main.py
