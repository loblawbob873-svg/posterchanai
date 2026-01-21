#!/bin/bash
# PosterchanAI Sync Client Installer - Cyberpunk Theme

set -e

# Colors for cyberpunk theme
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
MAGENTA='\033[0;35m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# ASCII Art Banner
echo -e "${CYAN}"
cat << "EOF"
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║     ██████╗  ██████╗ ███████╗████████╗███████╗██████╗       ║
║     ██╔══██╗██╔═══██╗██╔════╝╚══██╔══╝██╔════╝██╔══██╗      ║
║     ██████╔╝██║   ██║███████╗   ██║   █████╗  ██████╔╝      ║
║     ██╔═══╝ ██║   ██║╚════██║   ██║   ██╔══╝  ██╔══██╗      ║
║     ██║     ╚██████╔╝███████║   ██║   ███████╗██║  ██║      ║
║     ╚═╝      ╚═════╝ ╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝      ║
║                                                              ║
║              ███████╗██╗   ██╗███╗   ██╗ ██████╗             ║
║              ██╔════╝╚██╗ ██╔╝████╗  ██║██╔════╝             ║
║              ███████╗ ╚████╔╝ ██╔██╗ ██║██║                  ║
║              ╚════██║  ╚██╔╝  ██║╚██╗██║██║                  ║
║              ███████║   ██║   ██║ ╚████║╚██████╗             ║
║              ╚══════╝   ╚═╝   ╚═╝  ╚═══╝ ╚═════╝             ║
║                                                              ║
║                    CLIENT INSTALLER v1.0                     ║
╚══════════════════════════════════════════════════════════════╝
EOF
echo -e "${NC}"

# Check if running as user (not root)
if [ "$EUID" -eq 0 ]; then
    echo -e "${RED}${BOLD}[ERROR]${NC} Do not run as root! Run as your regular user."
    exit 1
fi

# Check for python3
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}${BOLD}[ERROR]${NC} python3 is not installed. Please install Python 3.8 or later."
    exit 1
fi

# Check Python version (need 3.8+)
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)
if [ "$PYTHON_MAJOR" -lt 3 ] || ([ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 8 ]); then
    echo -e "${RED}${BOLD}[ERROR]${NC} Python 3.8 or later is required. Found: $PYTHON_VERSION"
    exit 1
fi

# Get script directory (the git repo sync_client folder)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.config/posterchanai-sync"
SERVICE_DIR="$HOME/.config/systemd/user"

echo -e "${CYAN}${BOLD}[INFO]${NC} Installing PosterchanAI Sync Client..."
echo -e "${CYAN}${BOLD}[INFO]${NC} Running from: $SCRIPT_DIR"
echo ""

# Create config directory
echo -e "${GREEN}[1/3]${NC} Creating config directory..."
mkdir -p "$CONFIG_DIR"
mkdir -p "$SERVICE_DIR"
echo -e "${GREEN}✓${NC} Config directory created"

# Install dependencies (system python)
echo -e "${GREEN}[2/3]${NC} Checking dependencies..."
if ! python3 -c "import requests" 2>/dev/null; then
    echo -e "${YELLOW}Installing requests...${NC}"
    pip install --user requests || pip install requests
fi
echo -e "${GREEN}✓${NC} Dependencies OK"

# Install systemd service
echo -e "${GREEN}[3/3]${NC} Installing systemd user service..."
if [ ! -f "$SCRIPT_DIR/posterchanai-sync.service" ]; then
    echo -e "${YELLOW}⚠${NC} Service file not found, skipping service installation"
else
    cp "$SCRIPT_DIR/posterchanai-sync.service" "$SERVICE_DIR/"
    # Replace placeholder with actual script directory
    sed -i "s|SCRIPT_DIR_PLACEHOLDER|$SCRIPT_DIR|g" "$SERVICE_DIR/posterchanai-sync.service"
    # Reload systemd if available
    if command -v systemctl &> /dev/null && systemctl --user daemon-reload 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Systemd service installed"
    else
        echo -e "${YELLOW}⚠${NC} Systemd not available or not running, service file copied but not activated"
    fi
fi

# Create default config if it doesn't exist
if [ ! -f "$CONFIG_DIR/config.json" ]; then
    echo -e "${CYAN}[CONFIG]${NC} Run setup wizard: ${GREEN}python3 $SCRIPT_DIR/setup_wizard.py${NC}"
fi

echo ""
echo -e "${CYAN}${BOLD}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}[SUCCESS]${NC} Installation complete!"
echo ""
echo -e "${CYAN}Next steps:${NC}"
echo -e "  1. Run setup wizard (if not already configured):"
echo -e "     ${GREEN}python3 $SCRIPT_DIR/setup_wizard.py${NC}"
echo ""
echo -e "  2. Start the service:"
echo -e "     ${GREEN}systemctl --user start posterchanai-sync${NC}"
echo ""
echo -e "  3. Enable auto-start:"
echo -e "     ${GREEN}systemctl --user enable posterchanai-sync${NC}"
echo ""
echo -e "  4. View logs:"
echo -e "     ${GREEN}journalctl --user -u posterchanai-sync -f${NC}"
echo ""
echo -e "${CYAN}${BOLD}═══════════════════════════════════════════════════════════${NC}"
echo ""
