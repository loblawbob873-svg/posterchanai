#!/bin/bash
# Set up a local Telegram Bot API server (telegram-bot-api) so the bot can
# download/upload files up to ~2 GB instead of the cloud API's 20 MB cap.
#
# This is the ONE manual step behind the "Use local Bot API server" toggle in
# Admin -> Services -> Telegram Bot. Run it once on the host where the bot's
# webhook is served. After it finishes:
#   1. In Admin -> Services -> Telegram Bot, tick "Use local Bot API server"
#      and set the URL to  http://localhost:8081  (or this host's address).
#   2. Re-run "Setup Webhook" in the admin UI.
#
# Usage (simplest): enter API ID / API Hash in Admin -> Services -> Telegram Bot,
# save, then just run:
#   sudo ./scripts/setup-telegram-local-api.sh
# The script reads api_id / api_hash / bot_token from the posterchanai database,
# so there is nothing to type or edit. You can still override via env vars:
#   API_ID=123456 API_HASH=abcdef... [BOT_TOKEN=...] [PORT=8081] sudo -E ./scripts/setup-telegram-local-api.sh
#
# Get API ID / API Hash from https://my.telegram.org (API development tools).

set -euo pipefail

# Pull any values not supplied via env from the settings database.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${POSTERCHANAI_DB:-$REPO_DIR/posterchanai.db}"
_db_get() {
    [ -f "$DB_PATH" ] && command -v sqlite3 >/dev/null 2>&1 || return 0
    sqlite3 "$DB_PATH" "SELECT value FROM settings WHERE key='$1';" 2>/dev/null
}
API_ID="${API_ID:-$(_db_get telegram_api_id)}"
API_HASH="${API_HASH:-$(_db_get telegram_api_hash)}"
BOT_TOKEN="${BOT_TOKEN:-$(_db_get telegram_bot_token)}"

# Be forgiving of values pasted with their my.telegram.org labels (e.g.
# "App api_hash: 9a1e…") or stray whitespace — extract just the real value.
API_ID="$(printf '%s' "$API_ID" | grep -oE '[0-9]{4,}' | head -1)"
API_HASH="$(printf '%s' "$API_HASH" | grep -oiE '[0-9a-f]{32}' | head -1)"
BOT_TOKEN="$(printf '%s' "$BOT_TOKEN" | grep -oE '[0-9]+:[A-Za-z0-9_-]+' | head -1)"

PORT="${PORT:-8081}"
PREFIX="${PREFIX:-/usr/local}"
BUILD_DIR="${BUILD_DIR:-/tmp/telegram-bot-api-build}"
SERVICE_NAME="telegram-bot-api"
RUN_USER="${RUN_USER:-$(logname 2>/dev/null || echo "$SUDO_USER")}"
WORK_DIR="${WORK_DIR:-/var/lib/telegram-bot-api}"

err() { echo "ERROR: $*" >&2; exit 1; }
info() { echo -e "\033[1;34m==>\033[0m $*"; }

[ "$(id -u)" = "0" ] || err "Please run with sudo (needs to install a binary + systemd service)."
[ -n "${API_ID:-}" ]  || err "No API ID found. Enter it in Admin -> Services -> Telegram Bot (or pass API_ID=…). Get it from https://my.telegram.org"
[ -n "${API_HASH:-}" ] || err "No API Hash found. Enter it in Admin -> Services -> Telegram Bot (or pass API_HASH=…). Get it from https://my.telegram.org"
[ -n "$RUN_USER" ] || err "Could not determine a non-root user to run the service; set RUN_USER=..."

# ---------------------------------------------------------------------------
# 1. Install build dependencies (best effort per distro)
# ---------------------------------------------------------------------------
install_deps() {
    if command -v telegram-bot-api >/dev/null 2>&1; then
        info "telegram-bot-api already installed: $(command -v telegram-bot-api) — skipping build."
        return 1
    fi
    info "Installing build dependencies…"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update && apt-get install -y make git zlib1g-dev libssl-dev gperf cmake g++
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y gcc-c++ make git zlib-devel openssl-devel gperf cmake
    elif command -v pacman >/dev/null 2>&1; then
        pacman -S --needed --noconfirm base-devel git zlib openssl gperf cmake
    elif command -v emerge >/dev/null 2>&1; then
        echo "Gentoo: ensure these are installed: dev-vcs/git sys-libs/zlib dev-libs/openssl dev-util/gperf dev-build/cmake sys-devel/gcc"
    else
        echo "Unknown distro — install manually: git, cmake, g++, gperf, zlib dev, openssl dev"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 2. Build telegram-bot-api from source
# ---------------------------------------------------------------------------
build() {
    info "Cloning + building telegram-bot-api (this can take 10–20 min and ~2 GB RAM)…"
    rm -rf "$BUILD_DIR"
    git clone --recursive https://github.com/tdlib/telegram-bot-api.git "$BUILD_DIR"
    mkdir -p "$BUILD_DIR/build"
    cd "$BUILD_DIR/build"
    cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX:PATH="$PREFIX" ..
    cmake --build . --target install -j "$(nproc)"
    cd /
    rm -rf "$BUILD_DIR"
    info "Installed: $(command -v telegram-bot-api || echo "$PREFIX/bin/telegram-bot-api")"
}

if install_deps; then
    build
fi

BIN="$(command -v telegram-bot-api || echo "$PREFIX/bin/telegram-bot-api")"
[ -x "$BIN" ] || err "telegram-bot-api binary not found after build."

# ---------------------------------------------------------------------------
# 3. Optional: log the bot out of the cloud API so it can move to this server
# ---------------------------------------------------------------------------
if [ -n "${BOT_TOKEN:-}" ]; then
    info "Logging the bot out of the cloud API (required to switch servers)…"
    curl -fsS "https://api.telegram.org/bot${BOT_TOKEN}/logOut" >/dev/null \
        && echo "  logOut OK (wait ~10 min before the token works on the local server)" \
        || echo "  logOut returned an error (may already be logged out) — continuing."
fi

# ---------------------------------------------------------------------------
# 4. systemd service
# Run in HTTP mode (NOT --local): files are served over HTTP at
# /file/bot<token>/<path>, which is how posterchanai downloads them. This keeps
# the bot host and the API server decoupled (no shared filesystem needed) while
# still lifting the file-size limit to ~2 GB.
# ---------------------------------------------------------------------------
mkdir -p "$WORK_DIR"
chown "$RUN_USER" "$WORK_DIR"

info "Writing /etc/systemd/system/${SERVICE_NAME}.service"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Telegram Bot API server (local)
After=network-online.target
Wants=network-online.target

[Service]
User=${RUN_USER}
ExecStart=${BIN} --api-id=${API_ID} --api-hash=${API_HASH} --http-port=${PORT} --dir=${WORK_DIR}
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"
sleep 2
systemctl --no-pager --full status "${SERVICE_NAME}.service" | head -n 6 || true

cat <<EOF

✅ Local Bot API server is running on port ${PORT}.

Next steps:
  1. Admin -> Services -> Telegram Bot:
       • tick "Use local Bot API server"
       • set "Bot API server URL" to  http://localhost:${PORT}
       • Save settings
  2. Click "Setup Webhook" in the admin UI so Telegram talks to this server.

The bot can now process files up to ~2 GB on Telegram.
EOF
