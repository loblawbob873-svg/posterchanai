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
        echo "    Image Backend: external (ComfyUI)"
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
    if [ "$INSTALL_LLM" = "1" ] && [ "$LLM_BACKEND" != "ollama" ]; then
        echo "    1. Download a GGUF model to /var/lib/posterchanai/models/"
        echo "    2. Configure model path in Admin > AI Settings"
    else
        echo "    1. Configure Ollama URL in Admin > AI Settings"
    fi

    if [ "$INSTALL_IMAGE" = "1" ] && [ "$IMAGE_BACKEND" = "native" ]; then
        echo "    • Download a Stable Diffusion model (SDXL recommended)"
        echo "    • Configure image model in Admin > Image Generation"
    elif [ "$IMAGE_BACKEND" = "comfyui" ]; then
        echo "    • Configure ComfyUI URL in Admin > Image Generation"
    fi
    echo ""
    echo -e "  ${BOLD}RAG (Codebase Indexing):${NC}"
    echo "    • Create collections in Admin > RAG tab"
    echo "    • First query downloads ~90MB embedding model"
    echo ""
}

show_help() {
    echo "Posterchanai Installer"
    echo ""
    echo "Usage: ./install.sh [options]"
    echo ""
    echo "Options:"
    echo "  --help, -h       Show this help message"
    echo "  --packages       Show required packages for your distro"
    echo ""
    echo "Installation Types:"
    echo "  Full Stack       LLM + Image Generation (recommended)"
    echo "  LLM Only         Chat/text generation only"
    echo "  Image Only       Image generation only (use external LLM)"
    echo "  Lightweight      Web UI only (external Ollama + ComfyUI)"
    echo ""
    echo "LLM Backends:"
    echo "  Intel Arc        IPEX-LLM + llama.cpp SYCL"
    echo "  NVIDIA           llama.cpp CUDA"
    echo "  AMD              llama.cpp ROCm/HIP"
    echo "  CPU              llama.cpp (slow)"
    echo "  Ollama           External Ollama service"
    echo ""
    echo "Image Backends:"
    echo "  Native           Built-in diffusers (CUDA/XPU/ROCm/CPU)"
    echo "  ComfyUI          External ComfyUI service"
    echo ""
}
