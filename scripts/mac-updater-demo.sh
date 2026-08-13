#!/usr/bin/env bash
set -euo pipefail

# This harness runs natively on macOS for local testing and on Linux in
# GitHub Actions. In both cases Mini AIOS itself runs as a Linux container.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEV_ROOT="$REPO_ROOT/.dev/mac-updater"
DATA_DIR="$DEV_ROOT/aios-data"
UPDATER_STATE_DIR="$DEV_ROOT/updater-state"
KEY_DIR="$DEV_ROOT/keys"
FEED_DIR="$DEV_ROOT/feed"
TOKEN_FILE="$DEV_ROOT/updater-admin-token"
APP_ENV_FILE="$DEV_ROOT/app.env"
RELEASE_ENV="$DEV_ROOT/release.env"
COMPOSE_FILE="$DEV_ROOT/compose.yaml"
CONFIG_FILE="$DEV_ROOT/updater.toml"
UPDATER_BINARY="$DEV_ROOT/mini-aios-updater"
LAUNCH_AGENT="$DEV_ROOT/com.mahithsc.mini-aios-updater.plist"
REGISTRY_NAME="mini-aios-local-registry"
REGISTRY_PORT="5001"
IMAGE_REPOSITORY="localhost:${REGISTRY_PORT}/mini-aios"
MINIMUM_DEMO_FREE_BYTES=$((5 * 1024 * 1024 * 1024))

mkdir -p "$DATA_DIR" "$UPDATER_STATE_DIR" "$KEY_DIR" "$FEED_DIR" "$DEV_ROOT/logs"

if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop is not running." >&2
  exit 1
fi

AVAILABLE_BLOCKS="$(df -Pk "$REPO_ROOT" | awk 'NR == 2 {print $4}')"
AVAILABLE_BYTES=$((AVAILABLE_BLOCKS * 1024))
if [ "$AVAILABLE_BYTES" -lt "$MINIMUM_DEMO_FREE_BYTES" ]; then
  AVAILABLE_MIB=$((AVAILABLE_BYTES / 1024 / 1024))
  echo "Not enough free disk space for the Mac updater demo." >&2
  echo "Available: ${AVAILABLE_MIB} MiB; required: at least 5120 MiB." >&2
  echo "Free disk space, restart Docker Desktop if its storage became read-only, then run this command again." >&2
  exit 1
fi

case "$(uname -m)" in
  arm64) GO_ARCH="arm64" ;;
  x86_64) GO_ARCH="amd64" ;;
  *) echo "Unsupported host architecture: $(uname -m)" >&2; exit 1 ;;
esac
case "$(uname -s)" in
  Darwin) HOST_GOOS="darwin" ;;
  Linux) HOST_GOOS="linux" ;;
  *) echo "Unsupported host operating system: $(uname -s)" >&2; exit 1 ;;
esac
CONTAINER_PLATFORM="linux/${GO_ARCH}"
MANIFEST_PLATFORM="linux-${GO_ARCH}"

if docker container inspect "$REGISTRY_NAME" >/dev/null 2>&1; then
  docker start "$REGISTRY_NAME" >/dev/null
else
  docker run -d --name "$REGISTRY_NAME" -p "${REGISTRY_PORT}:5000" registry:2 >/dev/null
fi

if ! docker exec "$REGISTRY_NAME" sh -c \
  'probe=/var/lib/registry/.mini-aios-write-test; : > "$probe" && rm -f "$probe"'; then
  echo "The local test registry storage is not writable." >&2
  echo "Free disk space and restart Docker Desktop before retrying." >&2
  echo "If it remains read-only, recreate only the test registry with:" >&2
  echo "  docker rm -f $REGISTRY_NAME" >&2
  exit 1
fi

if [ ! -f "$KEY_DIR/private.pem" ]; then
  PYTHONPATH="$REPO_ROOT" uv run --project "$REPO_ROOT" --python 3.12 \
    python "$REPO_ROOT/release/publish_update.py" keygen \
    --private-key "$KEY_DIR/private.pem" \
    --public-key "$KEY_DIR/public.pem" >/dev/null
fi

if [ ! -f "$TOKEN_FILE" ]; then
  openssl rand -hex 32 > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
fi

# The current application imports billing/auth configuration at startup. These
# local-only placeholders let the updater exercise health and drain endpoints
# without making external Supabase or Stripe calls.
if [ ! -f "$APP_ENV_FILE" ]; then
  # The box holds no cloud secrets (billing/Supabase/Stripe live in aios-cloud),
  # so the app container boots from just its own config.
  printf '%s\n' \
    'SITE_URL_PROD=http://127.0.0.1:8765' > "$APP_ENV_FILE"
  chmod 600 "$APP_ENV_FILE"
fi

sed \
  -e "s|__CONTAINER_PLATFORM__|$CONTAINER_PLATFORM|g" \
  -e "s|__AIOS_DATA_DIR__|$DATA_DIR|g" \
  -e "s|__UPDATER_TOKEN_FILE__|$TOKEN_FILE|g" \
  -e "s|__APP_ENV_FILE__|$APP_ENV_FILE|g" \
  "$REPO_ROOT/updater/packaging/macos/compose.template.yaml" > "$COMPOSE_FILE"

DOCKER_BINARY="$(command -v docker)"
printf '%s\n' \
  'channel = "stable"' \
  "feed_url = \"file://${FEED_DIR}/stable.json\"" \
  "public_key_path = \"${KEY_DIR}/public.pem\"" \
  "allowed_image_repository = \"${IMAGE_REPOSITORY}\"" \
  "compose_project_dir = \"${DEV_ROOT}\"" \
  'compose_service = "box"' \
  "release_env_path = \"${RELEASE_ENV}\"" \
  "aios_data_dir = \"${DATA_DIR}\"" \
  'database_relative_path = "workspace/aios.db"' \
  "state_dir = \"${UPDATER_STATE_DIR}\"" \
  'health_url = "http://127.0.0.1:8765/internal/updater"' \
  "updater_token_file = \"${TOKEN_FILE}\"" \
  "docker_binary = \"${DOCKER_BINARY}\"" \
  'poll_interval = "24h"' \
  'poll_jitter = "0s"' \
  'minimum_free_bytes = 268435456' \
  'backup_retention = 2' \
  'maximum_drain_timeout = "2m"' \
  'maximum_startup_timeout = "2m"' \
  'maximum_observation_period = "2m"' \
  'clock_skew_allowance = "5m"' \
  'allow_development_host = true' > "$CONFIG_FILE"

(
  cd "$REPO_ROOT/updater"
  CGO_ENABLED=0 GOOS="$HOST_GOOS" GOARCH="$GO_ARCH" go build \
    -o "$UPDATER_BINARY" ./cmd/mini-aios-updater
)

if [ "$HOST_GOOS" = "darwin" ]; then
  sed \
    -e "s|__UPDATER_BINARY__|$UPDATER_BINARY|g" \
    -e "s|__UPDATER_CONFIG__|$CONFIG_FILE|g" \
    -e "s|__LOG_DIR__|$DEV_ROOT/logs|g" \
    "$REPO_ROOT/updater/packaging/macos/com.mahithsc.mini-aios-updater.plist.template" > "$LAUNCH_AGENT"
fi

if [ ! -f "$RELEASE_ENV" ]; then
  BASELINE_TAG="$IMAGE_REPOSITORY:baseline"
  docker build \
    --platform "$CONTAINER_PLATFORM" \
    --build-arg AIOS_VERSION=0.1.0-local \
    --build-arg AIOS_RELEASE_ID=local-baseline \
    --build-arg AIOS_RELEASE_SEQUENCE=0 \
    --build-arg AIOS_REVISION=local \
    -t "$BASELINE_TAG" "$REPO_ROOT"
  docker push "$BASELINE_TAG"
  BASELINE_REFERENCE="$(docker image inspect --format '{{index .RepoDigests 0}}' "$BASELINE_TAG")"
  BASELINE_DIGEST="${BASELINE_REFERENCE##*@}"
  printf '%s\n' \
    "AIOS_IMAGE=${IMAGE_REPOSITORY}@${BASELINE_DIGEST}" \
    'AIOS_RELEASE_ID=local-baseline' \
    'AIOS_VERSION=0.1.0-local' \
    'AIOS_RELEASE_SEQUENCE=0' \
    "AIOS_IMAGE_DIGEST=${BASELINE_DIGEST}" \
    'AIOS_REVISION=local' \
    'AIOS_DATABASE_SCHEMA=1' > "$RELEASE_ENV"
fi

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

SEQUENCE="$(date +%s)"
VERSION="0.1.0-local.${SEQUENCE}"
RELEASE_ID="local-${SEQUENCE}"
CANDIDATE_TAG="$IMAGE_REPOSITORY:$RELEASE_ID"

docker build \
  --platform "$CONTAINER_PLATFORM" \
  --build-arg "AIOS_VERSION=$VERSION" \
  --build-arg "AIOS_RELEASE_ID=$RELEASE_ID" \
  --build-arg "AIOS_RELEASE_SEQUENCE=$SEQUENCE" \
  --build-arg AIOS_REVISION=local \
  -t "$CANDIDATE_TAG" "$REPO_ROOT"
docker push "$CANDIDATE_TAG"
CANDIDATE_REFERENCE="$(docker image inspect --format '{{index .RepoDigests 0}}' "$CANDIDATE_TAG")"
CANDIDATE_DIGEST="${CANDIDATE_REFERENCE##*@}"

PUBLISHED_AT="$(uv run --no-project --python 3.12 python -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())')"
EXPIRES_AT="$(uv run --no-project --python 3.12 python -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc)+timedelta(days=1)).isoformat())')"

PYTHONPATH="$REPO_ROOT" uv run --project "$REPO_ROOT" --python 3.12 \
  python "$REPO_ROOT/release/publish_update.py" create-manifest \
  --output "$FEED_DIR/manifest.json" \
  --release-id "$RELEASE_ID" \
  --version "$VERSION" \
  --sequence "$SEQUENCE" \
  --channel stable \
  --published-at "$PUBLISHED_AT" \
  --expires-at "$EXPIRES_AT" \
  --revision local \
  --artifact "${MANIFEST_PLATFORM}=${IMAGE_REPOSITORY}@${CANDIDATE_DIGEST}:0" \
  --observation 30

PYTHONPATH="$REPO_ROOT" uv run --project "$REPO_ROOT" --python 3.12 \
  python "$REPO_ROOT/release/publish_update.py" sign \
  --manifest "$FEED_DIR/manifest.json" \
  --private-key "$KEY_DIR/private.pem" \
  --output "$FEED_DIR/stable.json"

echo "Checking signed update..."
"$UPDATER_BINARY" check --config "$CONFIG_FILE"
echo "Installing signed update; observation takes about 30 seconds..."
"$UPDATER_BINARY" install --config "$CONFIG_FILE" --release-id "$RELEASE_ID"
"$UPDATER_BINARY" status --config "$CONFIG_FILE"

echo
echo "Updater end-to-end demo completed on $HOST_GOOS/$GO_ARCH."
echo "AIOS: http://127.0.0.1:8765/health"
echo "Files: $DEV_ROOT"
if [ "$HOST_GOOS" = "darwin" ]; then
  echo "Optional background updater: launchctl bootstrap gui/$(id -u) $LAUNCH_AGENT"
fi
