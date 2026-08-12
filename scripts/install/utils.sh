#!/bin/bash
# Installer Utilities - Colors, print functions, banner
# Sourced by install.sh

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

print_banner() {
    echo -e "${CYAN}"
    echo -e "╔═══════════════════════════════════════════════════════════════╗"
    echo -e "║                                                               ║"
    echo -e "║   ${BOLD}POSTERCHANAI INSTALLER${NC}${CYAN}                                      ║"
    echo -e "║                                                               ║"
    echo -e "║   AI Chat + Image Generation                                  ║"
    echo -e "║                                                               ║"
    echo -e "╚═══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

print_step() {
    echo -e "\n${BLUE}▶ ${BOLD}$1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

print_summary() {
    echo ""
    echo -e "${GREEN}╔═══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  ${BOLD}Installation Complete!${NC}${GREEN}                                      ║${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    # Show component info
    echo -e "  ${BOLD}Components:${NC}"
    if [ "$INSTALL_LLM" = "1" ]; then
        echo "    LLM Backend: $LLM_BACKEND"
    else
        echo "    LLM Backend: external (Ollama)"
    fi
    if [ "$INSTALL_IMAGE" = "1" ]; then
        echo "    Image Backend: $IMAGE_BACKEND"
    else
        echo "    Image Backend: external (remote posterchanai image servers)"
    fi
    echo ""

    echo "  Service: $SERVICE_NAME"
    echo ""
    echo -e "  ${BOLD}Access:${NC} http://localhost:3051"
    echo -e "  ${BOLD}Login:${NC}  admin / admin"
    echo ""
    echo -e "  ${BOLD}Commands:${NC}"
    echo "    Start:   sudo systemctl start $SERVICE_NAME"
    echo "    Stop:    sudo systemctl stop $SERVICE_NAME"
    echo "    Logs:    sudo journalctl -u $SERVICE_NAME -f"
    echo ""

    # Show next steps based on configuration
    echo -e "  ${BOLD}Next Steps:${NC}"
    if [ "$INSTALL_LLM" = "1" ] && [ "$LLM_BACKEND" != "remote" ]; then
        echo "    1. Download a GGUF model to /var/lib/posterchanai/models/"
        echo "    2. Configure model path in Admin > AI Settings"
    else
        echo "    1. Configure Ollama URL in Admin > AI Settings"
    fi

    if [ "$INSTALL_IMAGE" = "1" ] && [ "$IMAGE_BACKEND" = "native" ]; then
        echo "    • Download a Stable Diffusion model (SDXL recommended)"
        echo "    • Configure image model in Admin > Image Generation"
    else
        echo "    • Configure remote image server URLs in Admin > Image Generation"
    fi
    echo ""
}

fix_ipex_execstack() {
    # Fix executable stack issue on hardened kernels (Gentoo, etc.)
    # Intel IPEX libraries are built with RWX (executable) stack which is
    # blocked by default on systems with strict memory protection.
    # This function clears the executable stack flag using scanelf or patchelf.

    echo "  Fixing IPEX library executable stack flags..."

    # Find the IPEX library directory
    local IPEX_LIB_DIR
    IPEX_LIB_DIR=$(python -c "import intel_extension_for_pytorch, os; print(os.path.dirname(intel_extension_for_pytorch.__file__) + '/lib')" 2>/dev/null)

    if [ -z "$IPEX_LIB_DIR" ] || [ ! -d "$IPEX_LIB_DIR" ]; then
        # Fallback: search in venv
        IPEX_LIB_DIR=$(find "$VIRTUAL_ENV/lib" -path "*/intel_extension_for_pytorch/lib" -type d 2>/dev/null | head -1)
    fi

    if [ -z "$IPEX_LIB_DIR" ] || [ ! -d "$IPEX_LIB_DIR" ]; then
        print_warning "Could not find IPEX library directory"
        return
    fi

    local FIXED=0

    # Method 1: Use scanelf from pax-utils (best for Gentoo/hardened systems)
    if command -v scanelf &>/dev/null; then
        # scanelf -Xe clears executable stack and shows which files were modified
        if sudo scanelf -Xe "$IPEX_LIB_DIR"/libintel-ext-pt*.so 2>/dev/null | grep -q '!'; then
            FIXED=1
            print_success "Fixed executable stack using scanelf"
        fi
    fi

    # Method 2: Use patchelf as fallback
    if [ "$FIXED" = "0" ] && command -v patchelf &>/dev/null; then
        for lib in "$IPEX_LIB_DIR"/libintel-ext-pt*.so; do
            if [ -f "$lib" ]; then
                patchelf --clear-execstack "$lib" 2>/dev/null && FIXED=1
            fi
        done
        if [ "$FIXED" = "1" ]; then
            print_success "Fixed executable stack using patchelf"
        fi
    fi

    if [ "$FIXED" = "0" ]; then
        print_warning "Could not fix executable stack - XPU may fail on hardened kernels"
        echo "    Install pax-utils (scanelf) or patchelf to fix this automatically"
    fi
}

show_help() {
    echo "Posterchanai Installer"
    echo ""
    echo "Usage: ./install.sh [options]"
    echo ""
    echo "Options:"
    echo "  --help, -h       Show this help message"
    echo "  --packages       Show required packages for your distro"
    echo "  --music          Set up the ACE-Step music server (venv-music) for musicgeni"
    echo "  --video          Install the videogeni (text-to-video) deps into the image venv"
    echo "  --turn           Build the built-in Pion TURN relay for voice/video calls"
    echo "  --stream         Install the built-in MediaMTX media server for OBS streaming"
    echo "  --searxng        Run this node's own SearXNG (web search for the AI, news, bots, Web Search)"
    echo "  --sandbox        Set up the per-user Debian Docker sandbox (docker group + base image)"
    echo "  --webxdc         Serve mini apps (.xdc games/polls) from xdc.<your-domain> — DNS + cert + vhost"
    echo ""
    echo "Installation Types:"
    echo "  Full Stack       LLM + Image Generation (recommended)"
    echo "  LLM Only         Chat/text generation only"
    echo "  Image Only       Image generation only (use external LLM)"
    echo "  Lightweight      Web UI only (external Ollama + remote posterchanai image servers)"
    echo ""
    echo "LLM Backends:"
    echo "  Intel Arc        IPEX-LLM + llama.cpp SYCL"
    echo "  NVIDIA           llama.cpp CUDA"
    echo "  AMD              llama.cpp ROCm/HIP"
    echo "  CPU              llama.cpp (slow)"
    echo "  Ollama           External Ollama service"
    echo ""
    echo "Image Backend:"
    echo "  Native           Built-in diffusers (CUDA/XPU/ROCm/CPU)"
    echo ""
}

# Ensure Intel Graphics Compiler 2.35.5 is installed system-wide (idempotent). It is NOT in any
# distro repo and is required by BOTH Arc services - the 14B/long-context LLM (__spirv_GroupBroadcast)
# and image gen >=768 (oneDNN conv). Called from the Intel LLM and image paths; skips if present.
ensure_igc_235() {
    # Accept IGC >= 2.35.5 (an exact match would DOWNGRADE newer cards/distros that already ship
    # a newer IGC). Find the highest installed libigc version and version-compare it to 2.35.5.
    local _igc
    _igc=$(find /usr/lib64 /usr/lib -maxdepth 2 -name 'libigc.so.*.*.*' 2>/dev/null \
           | sed -E 's/.*libigc\.so\.([0-9]+\.[0-9]+\.[0-9]+).*/\1/' | sort -V | tail -1)
    if [ -n "$_igc" ] && [ "$(printf '%s\n2.35.5\n' "$_igc" | sort -V | head -1)" = "2.35.5" ]; then
        print_success "Intel Graphics Compiler $_igc present (>= 2.35.5)"
        return 0
    fi
    print_warning "Intel Graphics Compiler >= 2.35.5 not detected${_igc:+ (found $_igc)}."
    echo "  It unblocks the 14B/long-context LLM and image gen >=768. Most distros don't package a"
    echo "  new-enough IGC, so install-igc.sh fetches a prebuilt one. (Gentoo ships >=2.37 via"
    echo "  'emerge dev-util/intel-graphics-compiler' — but if it errors with an llvm::AttributeMask"
    echo "  redefinition, the ebuild is picking lld from the wrong LLVM slot; pin it via"
    echo "  /etc/portage/env — see the project_igc_emerge_lld_slot note.)"
    local igc_script="$SCRIPT_DIR/scripts/install-igc.sh"
    if [ ! -x "$igc_script" ]; then
        echo "  Install with: sudo ./scripts/install-igc.sh --download"
        return 0
    fi
    # install-igc.sh (no flag) uses a staged /opt/igc-2.35.5 if present, else downloads.
    local run_igc
    read -p "  Install IGC 2.35.5 now? (backs up existing IGC; needs sudo) [Y/n]: " run_igc
    if [[ "$run_igc" =~ ^[Nn] ]]; then
        echo "  Skipped. Install later with: sudo $igc_script --download"
        return 0
    fi
    if sudo "$igc_script"; then
        print_success "IGC 2.35.5 installed"
    else
        print_warning "IGC install failed - run manually later: sudo $igc_script --download"
    fi
}
