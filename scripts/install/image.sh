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

    # Install PyTorch XPU and dependencies
    # NOTE: venv-xpu uses standard numpy (not numpy<2 like venv-ipex)
    # This avoids version conflicts between IPEX-LLM and PyTorch XPU
    echo "  Installing PyTorch XPU..."
    source "$SCRIPT_DIR/venv-xpu/bin/activate"
    pip install --upgrade pip -q

    # Install PyTorch XPU (from test channel for Intel Arc support)
    pip install torch torchvision --index-url https://download.pytorch.org/whl/test/xpu -q

    # Install image generation libraries
    pip install diffusers transformers accelerate safetensors -q

    # Install base app requirements (for the web server)
    pip install -r "$SCRIPT_DIR/requirements.txt" -q

    # Install face detection dependencies
    pip install onnxruntime huggingface_hub insightface opencv-python-headless mkl -q

    deactivate

    print_success "venv-xpu configured with PyTorch XPU and diffusers"
    echo ""
    echo "  Version isolation:"
    echo "    venv-ipex: IPEX-LLM with numpy<2 (chat/LLM)"
    echo "    venv-xpu:  PyTorch XPU with standard numpy (image gen)"
}
