#!/bin/bash
# Image Generation Dependencies
# Sourced by install.sh

setup_image_deps() {
    # Skip if not installing image features
    if [ "$INSTALL_IMAGE" = "0" ]; then
        return
    fi

    print_step "Installing image processing dependencies..."

    # Always install face detection dependencies
    echo "  Installing face detection dependencies (InsightFace, MKL)..."
    pip install onnxruntime huggingface_hub insightface opencv-python-headless mkl -q
    print_success "Face detection dependencies installed"

    # Skip diffusers if using ComfyUI backend
    if [ "$IMAGE_BACKEND" != "native" ]; then
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
        intel)
            echo "  Intel XPU: Using existing IPEX PyTorch from venv-ipex"
            print_success "PyTorch XPU ready"
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
    # Only for Intel Arc using IPEX-LLM for chat
    if [ "$BACKEND" != "intel" ] || [ "$IMAGE_BACKEND" != "native" ]; then
        return
    fi

    echo ""
    print_step "Intel Arc Dual-Instance Setup"
    echo ""
    echo "  You're using IPEX-LLM for chat on Intel Arc."
    echo "  For image generation, you can run a separate instance using PyTorch XPU."
    echo ""
    echo "  This sets up:"
    echo "    - Port 3051: Chat (IPEX-LLM) - current instance"
    echo "    - Port 3052: Images (PyTorch XPU) - separate instance"
    echo ""
    read -p "Set up XPU image generation instance? [y/N]: " SETUP_XPU_IMAGE
    SETUP_XPU_IMAGE=${SETUP_XPU_IMAGE:-N}

    if [[ ! "$SETUP_XPU_IMAGE" =~ ^[Yy] ]]; then
        print_warning "Skipping XPU image instance setup"
        echo "  You can set this up later with: ./scripts/setup-image-instance.sh"
        return
    fi

    print_step "Setting up XPU image instance..."

    # Create venv-xpu
    if [ ! -d "$SCRIPT_DIR/venv-xpu" ]; then
        echo "  Creating venv-xpu..."
        python3 -m venv "$SCRIPT_DIR/venv-xpu"
    fi

    # Install PyTorch XPU
    echo "  Installing PyTorch XPU..."
    source "$SCRIPT_DIR/venv-xpu/bin/activate"
    pip install --upgrade pip -q
    pip install torch torchvision --index-url https://download.pytorch.org/whl/test/xpu -q
    pip install diffusers transformers accelerate safetensors -q
    pip install -r "$SCRIPT_DIR/requirements.txt" -q
    pip install -r "$SCRIPT_DIR/requirements-image.txt" -q 2>/dev/null || true
    deactivate

    # Install systemd service
    if [ -d "$HOME/.config/systemd/user" ]; then
        cp "$SCRIPT_DIR/posterchanai-xpu-image.service" "$HOME/.config/systemd/user/" 2>/dev/null || true
        systemctl --user daemon-reload 2>/dev/null || true
        print_success "XPU image instance installed!"
        echo ""
        echo "  To enable: systemctl --user enable posterchanai-xpu-image"
        echo "  To start:  systemctl --user start posterchanai-xpu-image"
    fi
}
