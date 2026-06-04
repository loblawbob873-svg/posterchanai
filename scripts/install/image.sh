#!/bin/bash
# Image Generation Dependencies
# Sourced by install.sh

setup_image_deps() {
    # Skip if not installing image features
    if [ "$INSTALL_IMAGE" = "0" ]; then
        return
    fi

    # Intel Arc with native image: all deps go into venv-xpu via setup_xpu_image_instance()
    # Skip this function entirely for Intel - venv-xpu handles everything
    if [ "$BACKEND" = "intel" ] && [ "$IMAGE_BACKEND" = "native" ]; then
        print_step "Intel Arc: Image deps will be installed in venv-xpu"
        return
    fi

    # Activate the chat venv for non-Intel backends
    local VENV_NAME="${CHAT_VENV_NAME:-venv}"
    source "$SCRIPT_DIR/$VENV_NAME/bin/activate"

    print_step "Installing image processing dependencies..."

    # Always install face detection dependencies
    echo "  Installing face detection dependencies (InsightFace, MKL)..."
    pip install onnxruntime huggingface_hub insightface opencv-python-headless mkl -q
    print_success "Face detection dependencies installed"

    # Skip diffusers if using ComfyUI backend
    if [ "$IMAGE_BACKEND" != "native" ]; then
        deactivate
        print_success "Using ComfyUI backend - skipping diffusers installation"
        return
    fi

    print_step "Installing native image generation dependencies..."

    # Install diffusers/transformers/accelerate
    pip install diffusers transformers accelerate safetensors -q
    print_success "Diffusers installed"

    # Install GPU-specific PyTorch
    case "$GPU_TYPE" in
        nvidia)
            setup_pytorch_nvidia
            ;;
        amd)
            setup_pytorch_amd
            ;;
        *)
            setup_pytorch_cpu
            ;;
    esac

    # Optional: xformers for NVIDIA
    if [ "$GPU_TYPE" = "nvidia" ]; then
        read -p "Install xformers for faster image generation? [Y/n]: " INSTALL_XFORMERS
        INSTALL_XFORMERS=${INSTALL_XFORMERS:-Y}
        if [[ "$INSTALL_XFORMERS" =~ ^[Yy] ]]; then
            echo "  Installing xformers..."
            pip install xformers -q 2>/dev/null || print_warning "xformers install failed (optional)"
        fi
    fi

    deactivate
    print_success "Image generation dependencies installed"
}

setup_pytorch_nvidia() {
    if ! python3 -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
        echo "  Installing PyTorch with CUDA support..."
        pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121 -q
    fi
    print_success "PyTorch CUDA ready"
}

setup_pytorch_amd() {
    if python3 -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
        print_success "PyTorch ROCm already installed"
        return
    fi

    echo ""
    print_warning "Installing PyTorch with ROCm support..."
    echo "  This downloads ~5GB and may take 5-15 minutes."
    echo ""

    # Check disk space
    local AVAIL_GB
    AVAIL_GB=$(df -BG "$HOME" | awk 'NR==2 {print $4}' | tr -d 'G')
    if [ "$AVAIL_GB" -lt 10 ]; then
        print_error "Less than 10GB free disk space. Need ~10GB for PyTorch ROCm."
        exit 1
    fi

    # Use home dir for temp to avoid small /tmp issues
    mkdir -p "$HOME/tmp"
    TMPDIR="$HOME/tmp" TEMP="$HOME/tmp" TMP="$HOME/tmp" \
        pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/nightly/rocm7.0 || {
            print_error "PyTorch ROCm installation failed!"
            rm -rf "$HOME/tmp"
            exit 1
        }
    rm -rf "$HOME/tmp"

    # Verify installation
    if python3 -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
        local GFX_VER
        GFX_VER=$(python3 -c "import torch; print(torch.cuda.get_device_name(0))" 2>/dev/null || echo "Unknown")
        print_success "PyTorch ROCm installed: $GFX_VER"
    else
        print_warning "PyTorch installed but GPU not detected. Check ROCm installation."
    fi
}

setup_pytorch_cpu() {
    if ! python3 -c "import torch" 2>/dev/null; then
        echo "  Installing PyTorch (CPU)..."
        pip install torch torchvision -q
    fi
    print_success "PyTorch CPU ready"
}

setup_xpu_image_instance() {
    # Only for Intel Arc using IPEX-LLM for chat with native image generation
    if [ "$BACKEND" != "intel" ] || [ "$IMAGE_BACKEND" != "native" ]; then
        return
    fi

    echo ""
    print_step "Intel Arc Dual-Instance Setup"
    echo ""
    echo "  Setting up separate venvs for Intel Arc:"
    echo "    - venv-ipex: Chat (IPEX-LLM) - uses IPEX optimizations"
    echo "    - venv-xpu:  Images (PyTorch XPU) - uses native PyTorch XPU"
    echo ""
    echo "  Two systemd services will be created:"
    echo "    - posterchanai-ipex.service (port 3051) - Chat"
    echo "    - posterchanai-xpu-image.service (port 3052) - Images"
    echo ""

    print_step "Setting up venv-xpu for image generation..."

    # Export for use by systemd.sh
    export IMAGE_VENV_NAME="venv-xpu"

    # Create venv-xpu
    if [ ! -d "$SCRIPT_DIR/venv-xpu" ]; then
        echo "  Creating venv-xpu..."
        python3 -m venv "$SCRIPT_DIR/venv-xpu"
        print_success "Created venv-xpu"
    else
        print_success "venv-xpu already exists"
    fi

    # MODERN stack: native PyTorch 2.8 XPU - NO IPEX needed for diffusers. torch 2.8 bundles its
    # own oneAPI 2025.1 runtime via pip, so the launcher does NOT source a system oneAPI (that
    # would clash). This is the proven-working image stack (SDXL 1024 OK on IGC 2.35.5).
    echo "  Installing PyTorch 2.8 XPU (native, no IPEX)..."
    source "$SCRIPT_DIR/venv-xpu/bin/activate"
    pip install --upgrade pip -q

    # torch FIRST from the XPU index, then the image deps (pinned in requirements-image.txt:
    # diffusers>=0.38, transformers<5, accelerate, safetensors, pillow).
    pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/xpu -q || \
        pip install torch==2.8.0 --index-url https://download.pytorch.org/whl/test/xpu -q
    pip install -r "$SCRIPT_DIR/requirements-image.txt" -q

    # Base app requirements (the image instance also serves the web app on port 3052)
    pip install -r "$SCRIPT_DIR/requirements.txt" -q

    # Face detection / restore extras (optional features)
    pip install onnxruntime huggingface_hub insightface opencv-python-headless -q 2>/dev/null || \
        print_warning "optional face-detection deps failed (non-fatal)"

    deactivate

    print_success "venv-xpu configured with PyTorch 2.8 XPU + diffusers (modern, no IPEX)"
    echo ""
    # Image gen at >=768 needs IGC 2.35.5 (older IGC fails oneDNN "could not create a primitive").
    # Shared helper; idempotent, so this is a no-op if the LLM path already installed it.
    ensure_igc_235
}
