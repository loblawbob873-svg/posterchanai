#!/bin/bash

set -e

echo "Setting up Posterchanai..."

# Create upload directory
UPLOAD_PATH="/var/lib/posterchanai"
if [ ! -d "$UPLOAD_PATH" ]; then
    echo "Creating upload directory at $UPLOAD_PATH..."
    sudo mkdir -p "$UPLOAD_PATH"
    sudo chown $(whoami):$(whoami) "$UPLOAD_PATH"
    echo "Upload directory created."
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# Install requirements
echo "Installing dependencies..."
pip install -r requirements.txt

echo ""
echo "Note: install these system packages (the Docker image installs them for you):"
echo "  • ffmpeg          — REQUIRED for all video/audio effects (hava/curb/thug/…),"
echo "                      TTS/STT and yt-dlp post-processing."
echo "  • a bold TTF font — meme captions & effect text overlays (Impact / DejaVu-Bold /"
echo "                      Liberation-Sans-Bold); without one they fall back to a basic font."
echo "  • tesseract-ocr   — PDF OCR for scanned documents."
echo "  • tor + libtorrent — the built-in Tor proxy + torrent client are ON by default on a manual"
echo "                      install, so install these or they won't work (or turn them off in"
echo "                      Admin → Network). PosterChanAI runs + manages the tor daemon(s)"
echo "                      itself — two by default (US + Canada exits, load-balanced); it just"
echo "                      needs the system 'tor' binary + Python libtorrent on PATH."
echo "  Debian/Ubuntu: sudo apt install ffmpeg tesseract-ocr fonts-dejavu fonts-liberation tor python3-libtorrent"
echo "  Arch:          sudo pacman -S ffmpeg tesseract ttf-dejavu ttf-liberation tor python-libtorrent-rasterbar"
echo "  Gentoo:        sudo emerge media-video/ffmpeg app-text/tesseract media-fonts/dejavu net-vpn/tor net-libs/libtorrent-rasterbar"
echo ""
echo "Note: the 'thug' effect uses InsightFace landmarks for precise sunglasses/cig"
echo "placement; its model (buffalo_l) auto-downloads on first use (needs internet)."
echo "Without it, thug falls back to the OpenCV cascade — still works, slightly less precise."
echo ""
echo "Setup complete!"
echo ""
echo "To run the application:"
echo "  source venv/bin/activate"
echo "  python run.py"
echo ""
echo "Default login: admin / admin"
echo "Access at: http://localhost:8000"
echo "Uploads stored at: $UPLOAD_PATH"
