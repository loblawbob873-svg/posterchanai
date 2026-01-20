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

# Copy files
echo -e "${GREEN}[2/6]${NC} Copying files..."
cp "$SCRIPT_DIR/webdav_mount.py" "$INSTALL_DIR/"
cp "$SCRIPT_DIR/setup_wizard.py" "$INSTALL_DIR/" 2>/dev/null || true
cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/webdav_mount.py"
chmod +x "$INSTALL_DIR/setup_wizard.py" 2>/dev/null || true
echo -e "${GREEN}✓${NC} Files copied"

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
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip > /dev/null 2>&1
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" > /dev/null 2>&1
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
cp "$SCRIPT_DIR/posterchanai-sync.service" "$SERVICE_DIR/"
sed -i "s|%h|$HOME|g" "$SERVICE_DIR/posterchanai-sync.service"
sed -i "s|%i|$USER|g" "$SERVICE_DIR/posterchanai-sync.service"
systemctl --user daemon-reload
echo -e "${GREEN}✓${NC} Systemd service installed"

# Check for FUSE
echo -e "${CYAN}[CHECK]${NC} Checking for FUSE..."
if [ -e /dev/fuse ]; then
    echo -e "${GREEN}✓${NC} FUSE is available"
else
    echo -e "${YELLOW}⚠${NC} FUSE may not be available"
    echo -e "${YELLOW}   ${NC} Please install FUSE:"
    echo -e "${YELLOW}   ${NC}   - Gentoo: emerge sys-fs/fuse"
    echo -e "${YELLOW}   ${NC}   - Debian/Ubuntu: apt install fuse3"
    echo -e "${YELLOW}   ${NC}   - Arch: pacman -S fuse"
    echo -e "${YELLOW}   ${NC}   - Fedora: dnf install fuse"
    echo -e "${YELLOW}   ${NC} Note: Python packages (fusepy, requests) will be installed automatically"
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
echo -e "  1. ${YELLOW}${BOLD}IMPORTANT:${NC} Run the setup wizard first to configure the mount:"
echo -e "     ${GREEN}posterchanai-webdav-mount --setup${NC}"
echo -e "     ${CYAN}Note:${NC} This will prompt you for server URL, WebDAV URL, and password"
echo ""
echo -e "  2. After setup, start the service:"
echo -e "     ${GREEN}systemctl --user start posterchanai-sync${NC}"
echo ""
echo -e "  3. Enable auto-start:"
echo -e "     ${GREEN}systemctl --user enable posterchanai-sync${NC}"
echo ""
echo -e "  4. Check mount status:"
echo -e "     ${GREEN}posterchanai-webdav-mount --status${NC}"
echo ""
echo -e "  5. Check service status:"
echo -e "     ${GREEN}systemctl --user status posterchanai-sync${NC}"
echo ""
echo -e "  6. View logs:"
echo -e "     ${GREEN}journalctl --user -u posterchanai-sync -f${NC}"
echo ""
echo -e "${YELLOW}${BOLD}⚠ WARNING:${NC} Do not start the service before running setup!"
echo -e "   The service cannot show the setup wizard and will fail if config is missing."
echo ""
echo -e "${CYAN}${BOLD}═══════════════════════════════════════════════════════════${NC}"
echo ""
