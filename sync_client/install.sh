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

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.local/share/posterchanai-sync"
BIN_DIR="$HOME/.local/bin"
CONFIG_DIR="$HOME/.config/posterchanai-sync"
SERVICE_DIR="$HOME/.config/systemd/user"

echo -e "${CYAN}${BOLD}[INFO]${NC} Installing PosterchanAI Sync Client..."
echo ""

# Create directories
echo -e "${GREEN}[1/6]${NC} Creating directories..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$BIN_DIR"
mkdir -p "$CONFIG_DIR"
mkdir -p "$SERVICE_DIR"
mkdir -p "$CONFIG_DIR/logs"
echo -e "${GREEN}✓${NC} Directories created"

# Symlink files (so git pull updates are immediately available)
echo -e "${GREEN}[2/6]${NC} Creating symlinks to source files..."
if [ ! -f "$SCRIPT_DIR/webdav_mount.py" ]; then
    echo -e "${RED}${BOLD}[ERROR]${NC} webdav_mount.py not found in $SCRIPT_DIR"
    exit 1
fi
if [ ! -f "$SCRIPT_DIR/requirements.txt" ]; then
    echo -e "${RED}${BOLD}[ERROR]${NC} requirements.txt not found in $SCRIPT_DIR"
    exit 1
fi

# Remove old copies if they exist (replace with symlinks)
[ -f "$INSTALL_DIR/webdav_mount.py" ] && [ ! -L "$INSTALL_DIR/webdav_mount.py" ] && rm "$INSTALL_DIR/webdav_mount.py"
[ -f "$INSTALL_DIR/setup_wizard.py" ] && [ ! -L "$INSTALL_DIR/setup_wizard.py" ] && rm "$INSTALL_DIR/setup_wizard.py"
[ -f "$INSTALL_DIR/requirements.txt" ] && [ ! -L "$INSTALL_DIR/requirements.txt" ] && rm "$INSTALL_DIR/requirements.txt"

# Create symlinks to source files
ln -sf "$SCRIPT_DIR/webdav_mount.py" "$INSTALL_DIR/webdav_mount.py"
if [ -f "$SCRIPT_DIR/setup_wizard.py" ]; then
    ln -sf "$SCRIPT_DIR/setup_wizard.py" "$INSTALL_DIR/setup_wizard.py"
fi
ln -sf "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
echo -e "${GREEN}✓${NC} Symlinks created (git pull updates apply automatically)"

# Create virtual environment
echo -e "${GREEN}[3/6]${NC} Creating Python virtual environment..."
if [ -d "$INSTALL_DIR/venv" ]; then
    echo -e "${YELLOW}⚠${NC} Virtual environment already exists, skipping..."
else
    python3 -m venv "$INSTALL_DIR/venv"
    echo -e "${GREEN}✓${NC} Virtual environment created"
fi

# Install dependencies
echo -e "${GREEN}[4/6]${NC} Installing dependencies..."
if ! "$INSTALL_DIR/venv/bin/pip" install --upgrade pip > /dev/null 2>&1; then
    echo -e "${RED}${BOLD}[ERROR]${NC} Failed to upgrade pip"
    exit 1
fi
if ! "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" > /dev/null 2>&1; then
    echo -e "${RED}${BOLD}[ERROR]${NC} Failed to install dependencies. Check requirements.txt"
    exit 1
fi
echo -e "${GREEN}✓${NC} Dependencies installed"

# Create wrapper script
echo -e "${GREEN}[5/6]${NC} Creating wrapper script..."
cat > "$BIN_DIR/posterchanai-webdav-mount" << EOF
#!/bin/bash
cd "$INSTALL_DIR"
# Handle --setup flag to run setup wizard
if [ "\$1" = "--setup" ]; then
    "$INSTALL_DIR/venv/bin/python" setup_wizard.py --force
    exit \$?
fi
# All other arguments (including --mount, --unmount, --status) are passed to webdav_mount.py
"$INSTALL_DIR/venv/bin/python" webdav_mount.py "\$@"
EOF
chmod +x "$BIN_DIR/posterchanai-webdav-mount"
echo -e "${GREEN}✓${NC} Wrapper script created"

# Install systemd service
echo -e "${GREEN}[6/6]${NC} Installing systemd user service..."
if [ ! -f "$SCRIPT_DIR/posterchanai-sync.service" ]; then
    echo -e "${YELLOW}⚠${NC} Service file not found, skipping service installation"
else
    cp "$SCRIPT_DIR/posterchanai-sync.service" "$SERVICE_DIR/"
    # Replace %h with $HOME in service file
    if command -v sed &> /dev/null; then
        sed -i "s|%h|$HOME|g" "$SERVICE_DIR/posterchanai-sync.service" 2>/dev/null || \
        sed -i '' "s|%h|$HOME|g" "$SERVICE_DIR/posterchanai-sync.service" 2>/dev/null || true
    fi
    # Reload systemd if available
    if command -v systemctl &> /dev/null && systemctl --user daemon-reload 2>/dev/null; then
        echo -e "${GREEN}✓${NC} Systemd service installed"
    else
        echo -e "${YELLOW}⚠${NC} Systemd not available or not running, service file copied but not activated"
    fi
fi

# Check for systemd (optional but recommended)
echo -e "${CYAN}[CHECK]${NC} Checking for systemd..."
if command -v systemctl &> /dev/null; then
    echo -e "${GREEN}✓${NC} Systemd is available"
else
    echo -e "${YELLOW}⚠${NC} Systemd not found - service will not auto-start"
    echo -e "${YELLOW}   ${NC} You can still run the client manually:"
    echo -e "${YELLOW}   ${NC}   $BIN_DIR/posterchanai-webdav-mount"
fi

# Create default config if it doesn't exist (setup wizard will prompt user)
if [ ! -f "$CONFIG_DIR/config.json" ]; then
    echo -e "${CYAN}[CONFIG]${NC} Configuration will be created on first run via setup wizard."
    echo -e "${CYAN}[CONFIG]${NC} The setup wizard will prompt you for server URL, WebDAV URL, and password."
fi

echo ""
echo -e "${CYAN}${BOLD}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}[SUCCESS]${NC} Installation complete!"
echo ""
echo -e "${CYAN}Next steps:${NC}"
echo -e "  1. ${YELLOW}${BOLD}IMPORTANT:${NC} Run the setup wizard first to configure the sync client:"
echo -e "     ${GREEN}posterchanai-webdav-mount --setup${NC}"
echo -e "     ${CYAN}Note:${NC} This will prompt you for:"
echo -e "       - Server URL (e.g., https://ai.poster.place)"
echo -e "       - Username (your email address)"
echo -e "       - Password (your PosterchanAI password)"
echo -e "       - Mount point (default: ~/PosterchanAI-Mount)"
echo ""
if command -v systemctl &> /dev/null; then
    echo -e "  2. After setup, start the service:"
    echo -e "     ${GREEN}systemctl --user start posterchanai-sync${NC}"
    echo ""
    echo -e "  3. Enable auto-start:"
    echo -e "     ${GREEN}systemctl --user enable posterchanai-sync${NC}"
    echo ""
    echo -e "  4. Check service status:"
    echo -e "     ${GREEN}systemctl --user status posterchanai-sync${NC}"
    echo ""
    echo -e "  5. View logs:"
    echo -e "     ${GREEN}journalctl --user -u posterchanai-sync -f${NC}"
    echo ""
else
    echo -e "  2. Run the client manually:"
    echo -e "     ${GREEN}posterchanai-webdav-mount${NC}"
    echo ""
fi
echo -e "  ${CYAN}Check sync status:${NC}"
echo -e "     ${GREEN}posterchanai-webdav-mount --status${NC}"
echo ""
echo -e "${YELLOW}${BOLD}⚠ WARNING:${NC} Do not start the service before running setup!"
echo -e "   The service cannot show the setup wizard and will fail if config is missing."
echo ""
echo -e "${CYAN}${BOLD}═══════════════════════════════════════════════════════════${NC}"
echo ""
