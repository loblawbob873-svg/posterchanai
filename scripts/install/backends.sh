#!/bin/bash
# Backend Selection - LLM and Image generation backends
# Sourced by install.sh

# Global variables set by selection
INSTALL_LLM=0
INSTALL_IMAGE=0
INSTALL_MUSIC=0            # optional ACE-Step music server (musicgeni)
INSTALL_VIDEO=0            # optional native text-to-video deps (videogeni)
INSTALL_REGENI=0          # optional native instruction image-editing deps (regeni)
INSTALL_TELEGRAM_BOTAPI=0  # optional local Telegram Bot API server (large files)
TELEGRAM_ONLY=0            # option 5: set up ONLY the Bot API server (add-on)
UPDATE_ONLY=0             # option 6: update deps + Telegram server, then exit
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
    echo "     Image generation only (chat via a remote OpenAI-compatible server)"
    echo ""
    echo -e "  4) ${BOLD}Lightweight${NC}"
    echo "     Web UI only (chat + image via remote posterchanai servers)"
    echo ""
    echo -e "  5) ${BOLD}Telegram Bot API server${NC} (add-on only — for an existing install)"
    echo "     Sets up ONLY the local Telegram Bot API server (so the bot handles"
    echo "     files up to ~2 GB). Skips the rest of the installer — no deps, no"
    echo "     models, no GPU, no service changes. Compiles telegram-bot-api (~10-20 min)."
    echo ""
    echo -e "  6) ${BOLD}Update${NC} (existing install)"
    echo "     Safely upgrade posterchanai Python deps (Intel Arc pins protected)"
    echo "     and optionally rebuild the Telegram Bot API server, then restart."
    echo ""

    read -p "Select installation type [1-6, default=1]: " INSTALL_TYPE
    INSTALL_TYPE=${INSTALL_TYPE:-1}

    # Option 5 is a pure add-on for an existing install: set up ONLY the Telegram
    # Bot API server and skip everything else (deps, models, GPU, systemd).
    if [ "$INSTALL_TYPE" = "5" ]; then
        INSTALL_TELEGRAM_BOTAPI=1
        TELEGRAM_ONLY=1
        echo -e "  ${GREEN}✓ Telegram Bot API server add-on only (skipping the rest of the install)${NC}"
        return 0
    fi

    # Option 6 is update mode: refresh deps + Telegram server, then exit.
    if [ "$INSTALL_TYPE" = "6" ]; then
        UPDATE_ONLY=1
        echo -e "  ${GREEN}✓ Update mode (deps + Telegram Bot API server)${NC}"
        return 0
    fi

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
        *)
            INSTALL_LLM=1
            INSTALL_IMAGE=1
            ;;
    esac

    # If they picked a plain type (1-4), still offer the add-on.
    if [ "$INSTALL_TELEGRAM_BOTAPI" != "1" ]; then
        echo ""
        read -p "Also add option 5 (local Telegram Bot API server, for files >20MB)? [y/N]: " WANT_TG_BOTAPI
        if [[ "$WANT_TG_BOTAPI" =~ ^[Yy] ]]; then
            INSTALL_TELEGRAM_BOTAPI=1
            echo -e "  ${GREEN}✓ Will set up the local Telegram Bot API server${NC}"
        fi
    fi

    # Offer the ACE-Step music server (musicgeni) on any GPU install. It's a separate, heavy
    # add-on (own venv via uv, multi-GB model, systemd service), so it's opt-in.
    echo ""
    read -p "Set up music generation (ACE-Step / musicgeni)? Separate GPU service, ~9GB model [y/N]: " WANT_MUSIC
    if [[ "$WANT_MUSIC" =~ ^[Yy] ]]; then
        INSTALL_MUSIC=1
        echo -e "  ${GREEN}✓ Will set up the ACE-Step music server${NC}"
    fi

    # Offer text-to-video (videogeni). NATIVE diffusers — rides the image venv (no separate service),
    # just adds a couple deps + an optional model prefetch. Works on CUDA/Arc-XPU/ROCm.
    echo ""
    read -p "Set up video generation (videogeni)? Native diffusers, shares the image GPU, ~27GB model [y/N]: " WANT_VIDEO
    if [[ "$WANT_VIDEO" =~ ^[Yy] ]]; then
        INSTALL_VIDEO=1
        echo -e "  ${GREEN}✓ Will install video generation deps${NC}"
    fi

    # Offer instruction image editing (regeni). NATIVE diffusers (OmniGen v1) — rides the image venv,
    # no extra deps beyond the stack, ~9GB model. Works on CUDA/Arc-XPU/ROCm (fits 12-16GB).
    echo ""
    read -p "Set up image editing (regeni)? Native diffusers, shares the image GPU, ~9GB model [y/N]: " WANT_REGENI
    if [[ "$WANT_REGENI" =~ ^[Yy] ]]; then
        INSTALL_REGENI=1
        echo -e "  ${GREEN}✓ Will install image editing deps${NC}"
    fi
}

select_llm_backend() {
    # Skip if not installing LLM
    if [ "$INSTALL_LLM" = "0" ]; then
        LLM_BACKEND="remote"  # No local LLM — use a remote/external OpenAI-compatible server
        BACKEND="remote"
        return
    fi

    print_step "Select LLM inference backend:"
    echo ""
    echo -e "  1) ${BOLD}Intel Arc GPU${NC} (llama.cpp SYCL)"
    echo "     Best for Intel Arc A770, A750, A380, Battlemage, etc."
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

    # Default based on detection
    local DEFAULT=4
    case "$GPU_TYPE" in
        intel) DEFAULT=1 ;;
        nvidia) DEFAULT=2 ;;
        amd) DEFAULT=3 ;;
    esac

    read -p "Select LLM backend [1-4, default=$DEFAULT]: " BACKEND_CHOICE
    BACKEND_CHOICE=${BACKEND_CHOICE:-$DEFAULT}

    case "$BACKEND_CHOICE" in
        1) LLM_BACKEND="intel" ;;
        2) LLM_BACKEND="nvidia" ;;
        3) LLM_BACKEND="amd" ;;
        4) LLM_BACKEND="cpu" ;;
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
