#!/usr/bin/env bash
set -euo pipefail

# Bootstrap the first GitHub dev release, then install the newest signed one.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEV_ROOT="$REPO_ROOT/.dev/github-mac-updater"
DATA_DIR="$DEV_ROOT/aios-data"
STATE_DIR="$DEV_ROOT/updater-state"
TOKEN_FILE="$DEV_ROOT/updater-admin-token"
APP_ENV_FILE="$DEV_ROOT/app.env"
RELEASE_ENV="$DEV_ROOT/release.env"
COMPOSE_FILE="$DEV_ROOT/compose.yaml"
CONFIG_FILE="$DEV_ROOT/updater.toml"
UPDATER_BINARY="$DEV_ROOT/mini-aios-updater"
PUBLIC_KEY="$DEV_ROOT/update-signing-public.pem"
BASELINE_FEED="$DEV_ROOT/baseline.json"
LATEST_FEED="$DEV_ROOT/latest.json"
BASELINE_VERSION="0.1.0-dev.20260811.1"
BASELINE_URL="https://github.com/mahithsc/mini-aios/releases/download/v${BASELINE_VERSION}"
LATEST_URL="https://github.com/mahithsc/mini-aios/releases/latest/download"
FEED_URL="$LATEST_URL/dev.json"
MINIMUM_FREE_BYTES=$((5 * 1024 * 1024 * 1024))

for command_name in curl docker openssl python3; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is missing: $command_name" >&2
    exit 1
  fi
done

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This GitHub-hosted test script is intended for macOS." >&2
  exit 1
fi

case "$(uname -m)" in
  arm64) GO_ARCH="arm64" ;;
  x86_64) GO_ARCH="amd64" ;;
  *) echo "Unsupported Mac architecture: $(uname -m)" >&2; exit 1 ;;
esac

if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop is not running or its storage is unavailable." >&2
  exit 1
fi

AVAILABLE_BLOCKS="$(df -Pk "$REPO_ROOT" | awk 'NR == 2 {print $4}')"
AVAILABLE_BYTES=$((AVAILABLE_BLOCKS * 1024))
if [ "$AVAILABLE_BYTES" -lt "$MINIMUM_FREE_BYTES" ]; then
  AVAILABLE_MIB=$((AVAILABLE_BYTES / 1024 / 1024))
  echo "Not enough free disk space for the updater test." >&2
  echo "Available: ${AVAILABLE_MIB} MiB; required: at least 5120 MiB." >&2
  echo "Free disk space and restart Docker Desktop, then retry." >&2
  exit 1
fi

mkdir -p "$DATA_DIR" "$STATE_DIR"

curl --fail --location --silent --show-error "$FEED_URL" --output "$LATEST_FEED"
LATEST_VERSION="$(python3 - "$LATEST_FEED" <<'PY'
import base64
import json
import re
import sys
from pathlib import Path

envelope = json.loads(Path(sys.argv[1]).read_text())
manifest = json.loads(base64.b64decode(envelope["payload"], validate=True))
version = manifest["version"]
if not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", version):
    raise SystemExit("latest feed contains an unsafe version")
print(version)
PY
)"
curl --fail --location --silent --show-error \
  "$LATEST_URL/mini-aios-updater_${LATEST_VERSION}_darwin_${GO_ARCH}" \
  --output "$UPDATER_BINARY"
cp "$REPO_ROOT/updater/packaging/update-signing-public.pem" "$PUBLIC_KEY"
curl --fail --location --silent --show-error \
  "$BASELINE_URL/dev.json" \
  --output "$BASELINE_FEED"
chmod 755 "$UPDATER_BINARY"
chmod 644 "$PUBLIC_KEY"
xattr -d com.apple.quarantine "$UPDATER_BINARY" >/dev/null 2>&1 || true

if [ ! -f "$TOKEN_FILE" ]; then
  openssl rand -hex 32 > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi

if [ ! -f "$APP_ENV_FILE" ]; then
  printf '%s\n' \
    'SITE_URL_PROD=http://127.0.0.1:8765' \
    'STRIPE_PUBLISHABLE_KEY=pk_test_updater' \
    'STRIPE_SECRET_KEY=sk_test_updater' \
    'STRIPE_PRICE_ID=price_updater' \
    'SUPABASE_URL=http://127.0.0.1:54321' \
    'SUPABASE_SECRET_KEY=updater-test-key' > "$APP_ENV_FILE"
  chmod 600 "$APP_ENV_FILE"
fi

sed \
  -e "s|__CONTAINER_PLATFORM__|linux/$GO_ARCH|g" \
  -e "s|__AIOS_DATA_DIR__|$DATA_DIR|g" \
  -e "s|__UPDATER_TOKEN_FILE__|$TOKEN_FILE|g" \
  -e "s|__APP_ENV_FILE__|$APP_ENV_FILE|g" \
  "$REPO_ROOT/updater/packaging/macos/compose.template.yaml" > "$COMPOSE_FILE"

if [ ! -f "$RELEASE_ENV" ]; then
  python3 - "$BASELINE_FEED" "$RELEASE_ENV" "$GO_ARCH" <<'PY'
import base64
import json
import re
import sys
from pathlib import Path

feed_path, output_path, architecture = sys.argv[1:]
envelope = json.loads(Path(feed_path).read_text())
manifest = json.loads(base64.b64decode(envelope["payload"], validate=True))
artifact = manifest["artifacts"][f"linux-{architecture}"]
values = {
    "AIOS_IMAGE": f'{artifact["repository"]}@{artifact["digest"]}',
    "AIOS_RELEASE_ID": manifest["releaseId"],
    "AIOS_VERSION": manifest["version"],
    "AIOS_RELEASE_SEQUENCE": str(manifest["sequence"]),
    "AIOS_IMAGE_DIGEST": artifact["digest"],
    "AIOS_REVISION": manifest.get("revision", "unknown"),
    "AIOS_DATABASE_SCHEMA": str(manifest["database"]["toSchema"]),
}
safe = re.compile(r"^[A-Za-z0-9._:/@+-]+$")
if not all(safe.fullmatch(value) for value in values.values()):
    raise SystemExit("baseline feed contains an unsafe environment value")
Path(output_path).write_text("".join(f"{key}={value}\n" for key, value in values.items()))
PY
  chmod 600 "$RELEASE_ENV"
fi

DOCKER_BINARY="$(command -v docker)"
printf '%s\n' \
  'channel = "dev"' \
  "feed_url = \"$FEED_URL\"" \
  "public_key_path = \"$PUBLIC_KEY\"" \
  'allowed_image_repository = "ghcr.io/mahithsc/mini-aios"' \
  "compose_project_dir = \"$DEV_ROOT\"" \
  'compose_service = "box"' \
  "release_env_path = \"$RELEASE_ENV\"" \
  "aios_data_dir = \"$DATA_DIR\"" \
  'database_relative_path = "workspace/aios.db"' \
  "state_dir = \"$STATE_DIR\"" \
  'health_url = "http://127.0.0.1:8765/internal/updater"' \
  "updater_token_file = \"$TOKEN_FILE\"" \
  "docker_binary = \"$DOCKER_BINARY\"" \
  'poll_interval = "24h"' \
  'poll_jitter = "0s"' \
  'minimum_free_bytes = 268435456' \
  'backup_retention = 2' \
  'maximum_drain_timeout = "2m"' \
  'maximum_startup_timeout = "2m"' \
  'maximum_observation_period = "2m"' \
  'clock_skew_allowance = "5m"' \
  'allow_development_host = true' > "$CONFIG_FILE"

docker compose \
  --project-directory "$DEV_ROOT" \
  --env-file "$RELEASE_ENV" \
  up -d --no-build box

TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
for _ in $(seq 1 60); do
  if curl -fsS -H "Authorization: Bearer $TOKEN" \
    http://127.0.0.1:8765/internal/updater/ready >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8765/internal/updater/ready >/dev/null

RELEASE_ID="$(python3 - "$LATEST_FEED" <<'PY'
import base64
import json
import sys
from pathlib import Path

envelope = json.loads(Path(sys.argv[1]).read_text())
manifest = json.loads(base64.b64decode(envelope["payload"], validate=True))
print(manifest["releaseId"])
PY
)"

echo "Checking the GitHub-hosted signed update..."
"$UPDATER_BINARY" check --config "$CONFIG_FILE"
echo "Installing $RELEASE_ID; health observation takes about 30 seconds..."
"$UPDATER_BINARY" install --config "$CONFIG_FILE" --release-id "$RELEASE_ID"
"$UPDATER_BINARY" status --config "$CONFIG_FILE"

echo
echo "GitHub-hosted updater test completed."
echo "Mini AIOS: http://127.0.0.1:8765/health"
echo "Test files: $DEV_ROOT"
