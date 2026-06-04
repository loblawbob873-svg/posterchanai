#!/bin/bash
# Setup script for image-only posterchanai instance (Intel XPU)
# This creates a separate database configured for image generation only

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POSTERCHANAI_DIR="$(dirname "$SCRIPT_DIR")"
IMAGE_DB="$POSTERCHANAI_DIR/posterchanai-image.db"
# Modern image venv (torch 2.8 XPU, no IPEX). Falls back to legacy venv-xpu if that's all there is.
VENV_XPU="$POSTERCHANAI_DIR/venv-xpu-new"
[ -d "$VENV_XPU" ] || [ -d "$POSTERCHANAI_DIR/venv-xpu" ] && [ ! -d "$VENV_XPU" ] && VENV_XPU="$POSTERCHANAI_DIR/venv-xpu"

echo "Setting up image-only instance..."

# IGC >=2.35.5 is required for SDXL at >=768x768 (older IGC fails oneDNN "could not create a
# primitive"). Same IGC also unblocks the LLM 14B - install it once for both GPU services.
echo "NOTE: ensure Intel Graphics Compiler >=2.35.5 is installed (sudo $SCRIPT_DIR/install-igc.sh)"

# Create/populate the modern image venv (torch 2.8 XPU + diffusers). torch must be installed
# FIRST from the XPU index; it bundles its own oneAPI runtime so no IPEX is needed for diffusers.
if [ ! -d "$VENV_XPU" ]; then
    echo "Creating image venv at $VENV_XPU (torch 2.8 XPU)..."
    python3 -m venv "$VENV_XPU"
    "$VENV_XPU/bin/pip" install -q --upgrade pip
    "$VENV_XPU/bin/pip" install -q torch==2.8.0 --index-url https://download.pytorch.org/whl/xpu || true
fi
echo "Installing posterchanai requirements in $(basename "$VENV_XPU")..."
"$VENV_XPU/bin/pip" install -r "$POSTERCHANAI_DIR/requirements.txt" -q 2>/dev/null || true
echo "Installing image generation requirements..."
"$VENV_XPU/bin/pip" install -r "$POSTERCHANAI_DIR/requirements-image.txt" -q 2>/dev/null || true

# The launcher (run-xpu-image.sh) is machine-local (gitignored: run-*.sh); seed it from the
# committed template on first setup.
if [ ! -f "$POSTERCHANAI_DIR/run-xpu-image.sh" ] && [ -f "$POSTERCHANAI_DIR/run-xpu-image.sh.example" ]; then
    cp "$POSTERCHANAI_DIR/run-xpu-image.sh.example" "$POSTERCHANAI_DIR/run-xpu-image.sh"
    chmod +x "$POSTERCHANAI_DIR/run-xpu-image.sh"
    echo "Created run-xpu-image.sh from template."
fi

echo "Setting up image-only instance database..."

# Copy main database if it exists, otherwise start fresh
if [ -f "$POSTERCHANAI_DIR/posterchanai.db" ]; then
    echo "Copying settings from main database..."
    # The main DB runs in WAL mode, so recent writes may live in the -wal file. Use sqlite3's
    # online backup (WAL-aware) instead of a bare cp, which could copy a stale DB. Fall back to
    # checkpoint+cp if sqlite3 is unavailable.
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$POSTERCHANAI_DIR/posterchanai.db" ".backup '$IMAGE_DB'"
    else
        sqlite3 "$POSTERCHANAI_DIR/posterchanai.db" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
        cp "$POSTERCHANAI_DIR/posterchanai.db" "$IMAGE_DB"
    fi
else
    echo "No main database found, will create fresh database on first run"
    # Touch the file so we can update settings
    touch "$IMAGE_DB"
fi

# Configure image-only settings using sqlite3
if command -v sqlite3 &> /dev/null; then
    echo "Configuring image-only settings..."

    sqlite3 "$IMAGE_DB" << 'EOF'
-- Set VRAM mode to image_only (don't load LLM)
INSERT OR REPLACE INTO settings (key, value) VALUES ('vram_mode', 'image_only');

-- Use native diffusers backend with XPU
INSERT OR REPLACE INTO settings (key, value) VALUES ('image_backend', 'native');
INSERT OR REPLACE INTO settings (key, value) VALUES ('image_gpu_device', 'xpu');

-- Disable health check (image instance doesn't need LLM health check)
INSERT OR REPLACE INTO settings (key, value) VALUES ('ollama_ping_enabled', 'false');

-- Clear chat server URLs (this instance only handles images)
INSERT OR REPLACE INTO settings (key, value) VALUES ('chat_server_urls', '');
INSERT OR REPLACE INTO settings (key, value) VALUES ('image_server_urls', '');
EOF

    echo "Settings configured:"
    sqlite3 "$IMAGE_DB" "SELECT key, value FROM settings WHERE key IN ('vram_mode', 'image_backend', 'image_gpu_device', 'ollama_ping_enabled');"
else
    echo "WARNING: sqlite3 not found. Settings will need to be configured manually via admin panel."
fi

echo ""
echo "Image-only instance setup complete!"
echo ""
echo "To install the systemd service:"
echo "  cp $POSTERCHANAI_DIR/posterchanai-xpu-image.service ~/.config/systemd/user/"
echo "  systemctl --user daemon-reload"
echo "  systemctl --user enable posterchanai-xpu-image"
echo "  systemctl --user start posterchanai-xpu-image"
echo ""
echo "The image instance will run on port 3052"
echo "Configure other posterchanai servers to use http://$(hostname):3052 in image_server_urls"
