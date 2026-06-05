#!/bin/bash
#
# Posterchan Installer
# One-liner: curl -sSL https://git.poster.place/verita84/posterchan/raw/branch/master/install.sh | bash
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

print_banner() {
    echo -e "${CYAN}${BOLD}"
    echo "    ╔═══════════════════════════════════════════════════════════════╗"
    echo "    ║                                                               ║"
    echo "    ║   ██████╗  ██████╗ ███████╗████████╗███████╗██████╗           ║"
    echo "    ║   ██╔══██╗██╔═══██╗██╔════╝╚══██╔══╝██╔════╝██╔══██╗          ║"
    echo "    ║   ██████╔╝██║   ██║███████╗   ██║   █████╗  ██████╔╝          ║"
    echo "    ║   ██╔═══╝ ██║   ██║╚════██║   ██║   ██╔══╝  ██╔══██╗          ║"
    echo "    ║   ██║     ╚██████╔╝███████║   ██║   ███████╗██║  ██║          ║"
    echo "    ║   ╚═╝      ╚═════╝ ╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝          ║"
    echo "    ║                                                               ║"
    echo "    ║        ██████╗██╗  ██╗ █████╗ ███╗   ██╗                      ║"
    echo "    ║       ██╔════╝██║  ██║██╔══██╗████╗  ██║                      ║"
    echo "    ║       ██║     ███████║███████║██╔██╗ ██║                      ║"
    echo "    ║       ██║     ██╔══██║██╔══██║██║╚██╗██║                      ║"
    echo "    ║       ╚██████╗██║  ██║██║  ██║██║ ╚████║                      ║"
    echo "    ║        ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝                      ║"
    echo "    ║                                                               ║"
    echo -e "    ║             ${YELLOW}AI Bot for the Fediverse${CYAN}                        ║"
    echo "    ║                                                               ║"
    echo "    ╚═══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

info() {
    echo -e "${CYAN}ℹ ${NC}$1"
}

success() {
    echo -e "${GREEN}✓ ${NC}$1"
}

error() {
    echo -e "${RED}✗ ${NC}$1"
}

warning() {
    echo -e "${YELLOW}⚠ ${NC}$1"
}

print_banner

INSTALL_DIR="${POSTERCHAN_DIR:-$HOME/posterchan}"
REPO_URL="https://git.poster.place/verita84/posterchan.git"

# Update mode: `./install.sh --update` (or `update`) pulls latest code, upgrades
# dependencies, and restarts the service — skipping the interactive config.
# The bot has no GPU/IPEX-pinned deps, so upgrading is safe.
UPDATE_MODE=0
if [ "${1:-}" = "--update" ] || [ "${1:-}" = "update" ]; then
    UPDATE_MODE=1
fi

echo ""
info "Installation directory: $INSTALL_DIR"
echo ""

# Check for Python 3
if ! command -v python3 &> /dev/null; then
    error "Python 3 is required but not installed."
    echo ""
    echo "Install with:"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
    echo "  Fedora:        sudo dnf install python3 python3-pip"
    echo "  Arch:          sudo pacman -S python python-pip"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
success "Python $PYTHON_VERSION found"

# Check for git
if ! command -v git &> /dev/null; then
    error "Git is required but not installed."
    echo ""
    echo "Install with:"
    echo "  Ubuntu/Debian: sudo apt install git"
    echo "  Fedora:        sudo dnf install git"
    echo "  Arch:          sudo pacman -S git"
    exit 1
fi
success "Git found"

# Check for ffmpeg/ffprobe (required by tts.py for the /narrate feature).
# Non-fatal: the core bot runs without it, only TTS video output needs it.
if command -v ffmpeg &> /dev/null && command -v ffprobe &> /dev/null; then
    success "ffmpeg/ffprobe found"
else
    warning "ffmpeg/ffprobe not found — the /narrate (text-to-speech) feature will not work."
    echo ""
    echo "Install with:"
    echo "  Ubuntu/Debian: sudo apt install ffmpeg"
    echo "  Fedora:        sudo dnf install ffmpeg"
    echo "  Arch:          sudo pacman -S ffmpeg"
    echo ""
fi

# PostgreSQL is required for the database-backed features (blockbot, welcome,
# report, engagement). The bots auto-create their own tables on first run, but a
# running server + database must already exist and be reachable via the SQL_*
# settings you configure in install.py. The pip client (psycopg2-binary) is
# installed below, but the server itself is not.
if ! command -v psql &> /dev/null; then
    warning "PostgreSQL client (psql) not found — DB features (blockbot/welcome/report/engagement) need a Postgres server."
    echo ""
    echo "Install with:"
    echo "  Ubuntu/Debian: sudo apt install postgresql"
    echo "  Fedora:        sudo dnf install postgresql-server"
    echo "  Arch:          sudo pacman -S postgresql"
    echo ""
fi

# Clone or update repository
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Existing installation found, updating..."
    cd "$INSTALL_DIR"
    git pull
    success "Updated from git"
else
    info "Cloning repository..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    success "Repository cloned"
fi

cd "$INSTALL_DIR"

# Create virtual environment
if [ ! -d "venv" ]; then
    info "Creating virtual environment..."
    python3 -m venv venv
    success "Virtual environment created"
else
    success "Virtual environment exists"
fi

# Activate venv and install dependencies
info "Installing dependencies..."
source venv/bin/activate
pip install --upgrade pip -q
if [ "$UPDATE_MODE" = "1" ]; then
    pip install -r requirements.txt --upgrade -q
else
    pip install -r requirements.txt -q
fi
success "Dependencies installed"

# Update mode: restart the service and exit (no interactive config).
if [ "$UPDATE_MODE" = "1" ]; then
    echo ""
    if systemctl list-unit-files 2>/dev/null | grep -q '^posterchan\.service'; then
        info "Restarting posterchan service..."
        sudo systemctl restart posterchan 2>/dev/null && success "Restarted posterchan" \
            || warning "Could not restart posterchan — restart it manually."
    else
        info "No posterchan systemd service found — restart the bot manually if it's running."
    fi
    echo ""
    success "Update complete (code pulled, deps upgraded)."
    exit 0
fi

echo ""
echo -e "${GREEN}${BOLD}Installation complete!${NC}"
echo ""
echo "To configure your bot, run:"
echo -e "  ${CYAN}cd $INSTALL_DIR && python3 install.py${NC}"
echo ""
echo -e "To update later: ${CYAN}./install.sh --update${NC}"
echo ""

# Ask if user wants to run the interactive installer
read -p "Run interactive installer now? [Y/n] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
    python3 install.py
fi
