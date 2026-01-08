#!/bin/bash
# Setup script for image-only posterchanai instance (Intel XPU)
# This creates a separate database configured for image generation only

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POSTERCHANAI_DIR="$(dirname "$SCRIPT_DIR")"
IMAGE_DB="$POSTERCHANAI_DIR/posterchanai-image.db"
VENV_XPU="$POSTERCHANAI_DIR/venv-xpu"

echo "Setting up image-only instance..."

# Check/create venv-xpu and install dependencies
if [ -d "$VENV_XPU" ]; then
    echo "Installing posterchanai requirements in venv-xpu..."
    "$VENV_XPU/bin/pip" install -r "$POSTERCHANAI_DIR/requirements.txt" -q 2>/dev/null || true
    echo "Installing image generation requirements..."
    "$VENV_XPU/bin/pip" install -r "$POSTERCHANAI_DIR/requirements-image.txt" -q 2>/dev/null || true
else
    echo "WARNING: venv-xpu not found. Run the installer first or create venv-xpu manually."
fi

echo "Setting up image-only instance database..."

# Copy main database if it exists, otherwise start fresh
if [ -f "$POSTERCHANAI_DIR/posterchanai.db" ]; then
    echo "Copying settings from main database..."
    cp "$POSTERCHANAI_DIR/posterchanai.db" "$IMAGE_DB"
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
