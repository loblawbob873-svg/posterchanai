#!/bin/bash
# Set up a local Telegram Bot API server (telegram-bot-api) so the bot can
# download/upload files up to ~2 GB instead of the cloud API's 20 MB cap.
#
# You normally don't run this by hand — the posterchanai installer offers it as
# a prompt. It can also be run directly:
#   ./scripts/setup-telegram-local-api.sh          # reads creds from the DB
#   API_ID=123456 API_HASH=abcdef… [BOT_TOKEN=…] [PORT=8081] ./scripts/setup-telegram-local-api.sh
#
# It builds the server (as you), installs it + a systemd service (via sudo),
# then writes the settings so the web UI "just works" — no toggles to flip.
# Get API ID / API Hash from https://my.telegram.org (API development tools).

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The app + relay share ONE PostgreSQL database (no SQLite). libpq conninfo; matches the
# database.py default and NOSTR_RELAY_PG_DSN. Override via PG_DSN=… if your DB differs.
PG_DSN="${PG_DSN:-${NOSTR_RELAY_PG_DSN:-host=127.0.0.1 port=5432 dbname=posterchan_relay user=posterchan}}"

err()  { echo "ERROR: $*" >&2; exit 1; }
info() { echo -e "\033[1;34m==>\033[0m $*"; }

# Use sudo only for privileged steps; the heavy build runs as the current user.
SUDO=""
[ "$(id -u)" = "0" ] || SUDO="sudo"

command -v psql >/dev/null 2>&1 || err "psql is required (PostgreSQL client)."

# Escape single quotes for SQL string literals.
_sqlq() { printf "%s" "$1" | sed "s/'/''/g"; }
_db_get() {
    psql "$PG_DSN" -tAc "SELECT value FROM settings WHERE key='$(_sqlq "$1")';" 2>/dev/null
}
# NOTE: with settings_backend=relay the Setting table is hydrated from the relay on startup, so a
# direct write here can be reverted on the next restart — set telegram_api_base in Admin -> Telegram
# to persist it. The credential *reads* above are always correct.
_db_set() {
    psql "$PG_DSN" -c "INSERT INTO settings (key, value) VALUES ('$(_sqlq "$1")', '$(_sqlq "$2")') ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value;" >/dev/null 2>&1 || true
}

# Credentials: env overrides, else the database.
API_ID="${API_ID:-$(_db_get telegram_api_id)}"
API_HASH="${API_HASH:-$(_db_get telegram_api_hash)}"
BOT_TOKEN="${BOT_TOKEN:-$(_db_get telegram_bot_token)}"

# Be forgiving of values pasted with their my.telegram.org labels or whitespace.
API_ID="$(printf '%s' "$API_ID" | grep -oE '[0-9]{4,}' | head -1)"
API_HASH="$(printf '%s' "$API_HASH" | grep -oiE '[0-9a-f]{32}' | head -1)"
BOT_TOKEN="$(printf '%s' "$BOT_TOKEN" | grep -oE '[0-9]+:[A-Za-z0-9_-]+' | head -1)"

PORT="${PORT:-8081}"
PREFIX="${PREFIX:-/usr/local}"
BUILD_DIR="${BUILD_DIR:-/tmp/telegram-bot-api-build}"
SERVICE_NAME="telegram-bot-api"
RUN_USER="${RUN_USER:-${SUDO_USER:-$(id -un)}}"
WORK_DIR="${WORK_DIR:-/var/lib/telegram-bot-api}"

[ -n "${API_ID:-}" ]  || err "No API ID found (set it in Admin -> Telegram, or pass API_ID=…). Get it from https://my.telegram.org"
[ -n "${API_HASH:-}" ] || err "No API Hash found (set it in Admin -> Telegram, or pass API_HASH=…). Get it from https://my.telegram.org"

# ---------------------------------------------------------------------------
# 1. Build telegram-bot-api from source.
# Skipped if already installed, unless REBUILD=1 (to upgrade to a newer build).
# ---------------------------------------------------------------------------
if [ "${REBUILD:-0}" != "1" ] && command -v telegram-bot-api >/dev/null 2>&1; then
    info "telegram-bot-api already installed: $(command -v telegram-bot-api) — skipping build (set REBUILD=1 to rebuild/upgrade)."
else
    [ "${REBUILD:-0}" = "1" ] && info "REBUILD=1 — rebuilding telegram-bot-api from source."
    info "Installing build dependencies…"
    if command -v apt-get >/dev/null 2>&1; then
        $SUDO apt-get update && $SUDO apt-get install -y make git zlib1g-dev libssl-dev gperf cmake g++
    elif command -v dnf >/dev/null 2>&1; then
        $SUDO dnf install -y gcc-c++ make git zlib-devel openssl-devel gperf cmake
    elif command -v pacman >/dev/null 2>&1; then
        $SUDO pacman -S --needed --noconfirm base-devel git zlib openssl gperf cmake
    elif command -v emerge >/dev/null 2>&1; then
        echo "Gentoo: ensure installed: dev-vcs/git sys-libs/zlib dev-libs/openssl dev-util/gperf dev-build/cmake sys-devel/gcc"
    else
        echo "Unknown distro — install manually: git, cmake, g++, gperf, zlib dev, openssl dev"
    fi

    info "Cloning + building telegram-bot-api (can take 10–20 min and ~2 GB RAM)…"
    rm -rf "$BUILD_DIR"
    git clone --recursive https://github.com/tdlib/telegram-bot-api.git "$BUILD_DIR"
    mkdir -p "$BUILD_DIR/build"
    cd "$BUILD_DIR/build"
    cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX:PATH="$PREFIX" ..
    cmake --build . -j "$(nproc)"          # build as the current user
    $SUDO cmake --install .                 # install to $PREFIX (privileged)
    cd /
    rm -rf "$BUILD_DIR"
fi

BIN="$(command -v telegram-bot-api || echo "$PREFIX/bin/telegram-bot-api")"
[ -x "$BIN" ] || err "telegram-bot-api binary not found after build."
info "Using binary: $BIN"

# ---------------------------------------------------------------------------
# 2. Log the bot out of the cloud API so it can move to this server
# ---------------------------------------------------------------------------
if [ -n "${BOT_TOKEN:-}" ]; then
    info "Logging the bot out of the cloud API (required to switch servers)…"
    curl -fsS "https://api.telegram.org/bot${BOT_TOKEN}/logOut" >/dev/null \
        && echo "  logOut OK (the token may take a few minutes to work on the local server)" \
        || echo "  logOut returned an error (probably already logged out) — continuing."
fi

# ---------------------------------------------------------------------------
# 3. systemd service. Runs with --local so the bot can use a webhook pointing at
# a private/LAN address (e.g. a reverse proxy on 192.168.x.x) — the non-local
# server rejects "reserved" IPs. In --local mode getFile returns absolute file
# paths on disk, which posterchanai reads directly (same host/filesystem).
# ---------------------------------------------------------------------------
$SUDO mkdir -p "$WORK_DIR"
$SUDO chown "$RUN_USER" "$WORK_DIR"

info "Writing /etc/systemd/system/${SERVICE_NAME}.service"
$SUDO tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Telegram Bot API server (local)
After=network-online.target
Wants=network-online.target

[Service]
User=${RUN_USER}
ExecStart=${BIN} --local --api-id=${API_ID} --api-hash=${API_HASH} --http-port=${PORT} --dir=${WORK_DIR}
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now "${SERVICE_NAME}.service"
sleep 2

# ---------------------------------------------------------------------------
# 4. Make the web UI "just work": enable local mode + URL in settings,
#    and re-register the webhook on the local server if one is configured.
# ---------------------------------------------------------------------------
_db_set telegram_api_base "http://localhost:${PORT}"
_db_set telegram_local_api "true"

WEBHOOK_URL="$(_db_get telegram_webhook_url)"
if [ -n "$BOT_TOKEN" ] && [ -n "$WEBHOOK_URL" ]; then
    info "Registering webhook on the local server…"
    curl -fsS "http://localhost:${PORT}/bot${BOT_TOKEN}/setWebhook?url=$(printf '%s' "$WEBHOOK_URL" | sed 's/:/%3A/g; s#/#%2F#g')" >/dev/null 2>&1 \
        && echo "  webhook set." \
        || echo "  could not set webhook yet (the bot may need a few minutes after logOut) — re-run 'Setup Webhook' in the admin UI later."
fi

if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    cat <<EOF

✅ Local Bot API server is running on port ${PORT}, and the web UI is configured to use it.
   Telegram can now process files up to ~2 GB. Nothing else to do.
   (If a webhook was configured, give it a few minutes after logOut, or click
    "Setup Webhook" in Admin -> Telegram once.)
EOF
else
    err "Service failed to start. Check: ${SUDO} journalctl -u ${SERVICE_NAME} -n 50"
fi
