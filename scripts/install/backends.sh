#!/bin/bash
# Backend Selection - LLM and Image generation backends
# Sourced by install.sh

# Global variables set by selection
INSTALL_LLM=0
INSTALL_IMAGE=0
INSTALL_TUI=0
LLM_BACKEND=""
IMAGE_BACKEND=""
BACKEND=""  # Alias for LLM_BACKEND for backward compatibility

select_components() {
    print_step "Select installation type:"
    echo ""
    echo -e "  1) ${BOLD}Full Stack${NC} (Recommended)"
    echo "     LLM + Image Generation - Everything you need"
    echo ""
    echo -e "  2) ${BOLD}LLM Only${NC}"
    echo "     Chat and text generation only (no image features)"
    echo ""
    echo -e "  3) ${BOLD}Image Only${NC}"
    echo "     Image generation only (use external LLM like Ollama)"
    echo ""
    echo -e "  4) ${BOLD}Lightweight${NC}"
    echo "     Web UI only (use external Ollama + ComfyUI)"
    echo ""
    echo -e "  5) ${BOLD}TUI Only${NC}"
    echo "     Terminal UI client only (connects to existing server)"
    echo ""

    read -p "Select installation type [1-5, default=1]: " INSTALL_TYPE
    INSTALL_TYPE=${INSTALL_TYPE:-1}

    case "$INSTALL_TYPE" in
        1)
            INSTALL_LLM=1
            INSTALL_IMAGE=1
            echo -e "  ${GREEN}✓ Full Stack: LLM + Image Generation${NC}"
            ;;
        2)
            INSTALL_LLM=1
            INSTALL_IMAGE=0
            echo -e "  ${GREEN}✓ LLM Only${NC}"
            ;;
        3)
            INSTALL_LLM=0
            INSTALL_IMAGE=1
            echo -e "  ${GREEN}✓ Image Only${NC}"
            ;;
        4)
            INSTALL_LLM=0
            INSTALL_IMAGE=0
            echo -e "  ${GREEN}✓ Lightweight (external services)${NC}"
            ;;
        5)
            INSTALL_LLM=0
            INSTALL_IMAGE=0
            INSTALL_TUI=1
            echo -e "  ${GREEN}✓ TUI Only (Terminal Client)${NC}"
            return  # Skip other component questions
            ;;
        *)
            INSTALL_LLM=1
            INSTALL_IMAGE=1
            ;;
    esac

    # Ask about TUI for non-TUI-only installations
    echo ""
    read -p "Also install Terminal UI client? [y/N]: " INSTALL_TUI_CHOICE
    if [[ "$INSTALL_TUI_CHOICE" =~ ^[Yy] ]]; then
        INSTALL_TUI=1
        echo -e "  ${GREEN}✓ TUI will be installed${NC}"
    fi
}

select_llm_backend() {
    # Skip if not installing LLM
    if [ "$INSTALL_LLM" = "0" ]; then
        LLM_BACKEND="ollama"  # Default to external Ollama
        BACKEND="ollama"
        return
    fi

    print_step "Select LLM inference backend:"
    echo ""
    echo -e "  1) ${BOLD}Intel Arc GPU${NC} (IPEX-LLM + llama.cpp SYCL)"
    echo "     Best for Intel Arc A770, A750, A380, etc."
    echo ""
    echo -e "  2) ${BOLD}NVIDIA GPU${NC} (llama.cpp CUDA)"
    echo "     Best for GeForce RTX, Tesla, etc."
    echo ""
    echo -e "  3) ${BOLD}AMD GPU${NC} (llama.cpp ROCm/HIP)"
    echo "     Best for Radeon RX 6000/7000 series"
    echo -e "     ${YELLOW}Note: Requires ROCm installed, ~5GB download for PyTorch${NC}"
    echo ""
    echo -e "  4) ${BOLD}CPU Only${NC} (llama.cpp)"
    echo "     Works on any system, slower inference"
    echo ""
    echo -e "  5) ${BOLD}Ollama${NC} (External service)"
    echo "     Use existing Ollama installation"
    echo ""

    # Default based on detection
    local DEFAULT=4
    case "$GPU_TYPE" in
        intel) DEFAULT=1 ;;
        nvidia) DEFAULT=2 ;;
        amd) DEFAULT=3 ;;
    esac

    read -p "Select LLM backend [1-5, default=$DEFAULT]: " BACKEND_CHOICE
    BACKEND_CHOICE=${BACKEND_CHOICE:-$DEFAULT}

    case "$BACKEND_CHOICE" in
        1) LLM_BACKEND="intel" ;;
        2) LLM_BACKEND="nvidia" ;;
        3) LLM_BACKEND="amd" ;;
        4) LLM_BACKEND="cpu" ;;
        5) LLM_BACKEND="ollama" ;;
        *) LLM_BACKEND="cpu" ;;
    esac

    # Set BACKEND for backward compatibility
    BACKEND="$LLM_BACKEND"
}

select_image_backend() {
    # Skip if not installing image generation
    if [ "$INSTALL_IMAGE" = "0" ]; then
        IMAGE_BACKEND="comfyui"  # Default to external ComfyUI
        return
    fi

    print_step "Select image generation backend:"
    echo ""
    echo -e "  1) ${BOLD}Native (diffusers)${NC} - Recommended"
    echo "     Built-in Stable Diffusion using HuggingFace diffusers"
    echo "     Supports: NVIDIA (CUDA), Intel Arc (XPU), AMD (ROCm), CPU"
    echo ""
    echo -e "  2) ${BOLD}ComfyUI${NC} (External)"
    echo "     Use existing ComfyUI installation"
    echo "     More features but requires separate setup"
    echo ""

    read -p "Select image backend [1-2, default=1]: " IMAGE_CHOICE
    IMAGE_CHOICE=${IMAGE_CHOICE:-1}

    case "$IMAGE_CHOICE" in
        1) IMAGE_BACKEND="native" ;;
        2) IMAGE_BACKEND="comfyui" ;;
        *) IMAGE_BACKEND="native" ;;
    esac

    # Show AMD-specific warnings for native image generation
    if [ "$IMAGE_BACKEND" = "native" ] && [ "$GPU_TYPE" = "amd" ]; then
        echo ""
        print_warning "AMD ROCm Image Generation Notes:"
        echo ""
        echo "  • PyTorch ROCm nightly is ~5GB download"
        echo "  • Requires 10GB+ disk space for installation"
        echo "  • SDXL models need 8GB+ VRAM (12GB recommended)"
        echo "  • First generation is slow (shader compilation)"
        echo ""
        read -p "Continue with native image generation? [Y/n]: " CONTINUE_AMD
        if [[ "$CONTINUE_AMD" =~ ^[Nn] ]]; then
            IMAGE_BACKEND="comfyui"
            echo "  Switched to ComfyUI backend"
        fi
    fi
}
