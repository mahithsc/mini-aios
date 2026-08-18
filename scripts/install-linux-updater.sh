#!/usr/bin/env bash
set -euo pipefail

REPOSITORY="mahithsc/mini-aios"
RELEASE_BASE_URL="${MINI_AIOS_RELEASE_BASE_URL:-https://github.com/${REPOSITORY}/releases/latest/download}"
CHANNEL="dev"
FEED_URL=""
APP_ENV_SOURCE=""
INSTALL_ROOT="/"
NO_START=0
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMP_DIR=""

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/install-linux-updater.sh [options]

Installs and bootstraps Mini AIOS plus its host updater on a fresh Linux device.

Options:
  --app-env PATH   Optional root-owned application environment file to install.
                   The box holds no cloud secrets, so this is not required.
  --channel NAME   Signed feed channel: dev, beta, or stable (default: dev).
  --feed-url URL   Override the signed channel feed URL.
  --root PATH      Stage files under an offline root filesystem; implies --no-start.
  --no-start       Install files but do not bootstrap or enable systemd.
  -h, --help       Show this help.

Docker Engine with the Compose plugin must already be installed on a live device.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --app-env)
      [ "$#" -ge 2 ] || { echo "--app-env requires a path" >&2; exit 2; }
      APP_ENV_SOURCE="$2"
      shift 2
      ;;
    --channel)
      [ "$#" -ge 2 ] || { echo "--channel requires a value" >&2; exit 2; }
      CHANNEL="$2"
      shift 2
      ;;
    --feed-url)
      [ "$#" -ge 2 ] || { echo "--feed-url requires a URL" >&2; exit 2; }
      FEED_URL="$2"
      shift 2
      ;;
    --root)
      [ "$#" -ge 2 ] || { echo "--root requires a path" >&2; exit 2; }
      INSTALL_ROOT="$2"
      NO_START=1
      shift 2
      ;;
    --no-start)
      NO_START=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$CHANNEL" in
  dev|beta|stable) ;;
  *) echo "Unsupported channel: $CHANNEL" >&2; exit 2 ;;
esac

if [ -z "$FEED_URL" ]; then
  FEED_URL="$RELEASE_BASE_URL/$CHANNEL.json"
fi

case "$(uname -s)" in
  Linux) ;;
  *) echo "This installer must run on Linux." >&2; exit 1 ;;
esac

case "$(uname -m)" in
  x86_64|amd64) ARCH="amd64" ;;
  aarch64|arm64) ARCH="arm64" ;;
  *) echo "Unsupported Linux architecture: $(uname -m)" >&2; exit 1 ;;
esac

if [ "$INSTALL_ROOT" = "/" ] && [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root (for example, with sudo)." >&2
  exit 1
fi

command -v install >/dev/null 2>&1 || {
  echo "Required command is missing: install" >&2
  exit 1
}

target_path() {
  if [ "$INSTALL_ROOT" = "/" ]; then
    printf '%s' "$1"
  else
    printf '%s%s' "${INSTALL_ROOT%/}" "$1"
  fi
}

cleanup() {
  if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
    rm -rf -- "$TEMP_DIR"
  fi
}
trap cleanup EXIT
TEMP_DIR="$(mktemp -d)"

download_asset() {
  local asset_name="$1"
  local output_path="$2"
  command -v curl >/dev/null 2>&1 || {
    echo "Required command is missing: curl" >&2
    exit 1
  }
  curl --fail --location --silent --show-error \
    --proto '=https' --tlsv1.2 \
    "$RELEASE_BASE_URL/$asset_name" \
    --output "$output_path"
}

UPDATER_SOURCE="${MINI_AIOS_UPDATER_BINARY:-}"
if [ -z "$UPDATER_SOURCE" ] && [ -f "$REPO_ROOT/updater/go.mod" ] && command -v go >/dev/null 2>&1; then
  UPDATER_SOURCE="$TEMP_DIR/mini-aios-updater"
  (
    cd "$REPO_ROOT/updater"
    CGO_ENABLED=0 GOOS=linux GOARCH="$ARCH" go build \
      -trimpath -o "$UPDATER_SOURCE" ./cmd/mini-aios-updater
  )
elif [ -z "$UPDATER_SOURCE" ]; then
  UPDATER_SOURCE="$TEMP_DIR/mini-aios-updater"
  download_asset "mini-aios-updater_linux_${ARCH}" "$UPDATER_SOURCE"
fi

COMPOSE_SOURCE="$REPO_ROOT/updater/packaging/linux/compose.yaml"
if [ ! -f "$COMPOSE_SOURCE" ]; then
  COMPOSE_SOURCE="$TEMP_DIR/compose.yaml"
  download_asset "mini-aios-compose.yaml" "$COMPOSE_SOURCE"
fi

SERVICE_SOURCE="$REPO_ROOT/updater/packaging/linux/mini-aios-updater.service"
if [ ! -f "$SERVICE_SOURCE" ]; then
  SERVICE_SOURCE="$TEMP_DIR/mini-aios-updater.service"
  download_asset "mini-aios-updater.service" "$SERVICE_SOURCE"
fi

PUBLIC_KEY_SOURCE="$REPO_ROOT/updater/packaging/update-signing-public.pem"
if [ ! -f "$PUBLIC_KEY_SOURCE" ]; then
  PUBLIC_KEY_SOURCE="$TEMP_DIR/update-signing-public.pem"
  download_asset "update-signing-public.pem" "$PUBLIC_KEY_SOURCE"
fi

ETC_DIR="$(target_path /etc/mini-aios)"
OPT_DIR="$(target_path /opt/mini-aios)"
DATA_DIR="$(target_path /var/lib/mini-aios)"
STATE_DIR="$(target_path /var/lib/mini-aios-updater)"
BIN_PATH="$(target_path /usr/local/bin/mini-aios-updater)"
SERVICE_PATH="$(target_path /etc/systemd/system/mini-aios-updater.service)"

install -d -m 0700 "$ETC_DIR" "$DATA_DIR" "$STATE_DIR"
install -d -m 0755 "$OPT_DIR" "$(dirname "$BIN_PATH")" "$(dirname "$SERVICE_PATH")"
install -m 0755 "$UPDATER_SOURCE" "$BIN_PATH"
install -m 0644 "$COMPOSE_SOURCE" "$OPT_DIR/compose.yaml"
install -m 0644 "$SERVICE_SOURCE" "$SERVICE_PATH"
install -m 0644 "$PUBLIC_KEY_SOURCE" "$ETC_DIR/update-signing-public.pem"

# The box holds no cloud secrets — billing, Supabase, and Stripe live in
# aios-cloud. --app-env stays supported for optional local config, but a fresh
# device needs none of it to install and run.
APP_ENV_DESTINATION="$ETC_DIR/app.env"
if [ ! -f "$APP_ENV_DESTINATION" ] && [ -n "$APP_ENV_SOURCE" ]; then
  [ -f "$APP_ENV_SOURCE" ] || { echo "Application environment file not found: $APP_ENV_SOURCE" >&2; exit 1; }
  install -m 0600 "$APP_ENV_SOURCE" "$APP_ENV_DESTINATION"
fi

TOKEN_PATH="$ETC_DIR/updater-admin-token"
if [ ! -f "$TOKEN_PATH" ]; then
  umask 077
  head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$TOKEN_PATH"
fi
chmod 0600 "$TOKEN_PATH"

DOCKER_BINARY="/usr/bin/docker"
if [ "$NO_START" -eq 0 ]; then
  for command_name in docker systemctl; do
    command -v "$command_name" >/dev/null 2>&1 || {
      echo "Required command is missing: $command_name" >&2
      exit 1
    }
  done
  DOCKER_BINARY="$(command -v docker)"
  docker info >/dev/null
  docker compose version >/dev/null
fi

cat > "$ETC_DIR/updater.toml" <<EOF
channel = "$CHANNEL"
feed_url = "$FEED_URL"
public_key_path = "/etc/mini-aios/update-signing-public.pem"
allowed_image_repository = "ghcr.io/mahithsc/mini-aios"
compose_project_dir = "/opt/mini-aios"
compose_service = "box"
release_env_path = "/opt/mini-aios/release.env"
aios_data_dir = "/var/lib/mini-aios"
database_relative_path = "state/aios.db"
state_dir = "/var/lib/mini-aios-updater"
health_url = "http://127.0.0.1:8765/internal/updater"
updater_token_file = "/etc/mini-aios/updater-admin-token"
docker_binary = "$DOCKER_BINARY"
poll_interval = "30m"
poll_jitter = "30m"
minimum_free_bytes = 2147483648
backup_retention = 2
maximum_drain_timeout = "10m"
maximum_startup_timeout = "5m"
maximum_observation_period = "30m"
clock_skew_allowance = "5m"
allow_development_host = false
EOF
chmod 0600 "$ETC_DIR/updater.toml"

if [ "$NO_START" -eq 1 ]; then
  echo "Mini AIOS updater files staged under $INSTALL_ROOT; activation was skipped."
  exit 0
fi

if systemctl is-active --quiet mini-aios-updater 2>/dev/null; then
  systemctl stop mini-aios-updater
fi

if [ ! -f /opt/mini-aios/release.env ]; then
  echo "Bootstrapping the first signed Mini AIOS release..."
  /usr/local/bin/mini-aios-updater bootstrap --config /etc/mini-aios/updater.toml
else
  echo "Existing selected release found; preserving it."
  docker compose \
    --project-directory /opt/mini-aios \
    --env-file /opt/mini-aios/release.env \
    up -d --no-build box
fi

systemctl daemon-reload
systemctl enable --now mini-aios-updater
/usr/local/bin/mini-aios-updater doctor --config /etc/mini-aios/updater.toml

echo "Mini AIOS and its updater are installed."
echo "The updater is enabled for this boot and future Linux boots."
