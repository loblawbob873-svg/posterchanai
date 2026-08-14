#!/bin/bash
# Image Generation Dependencies
# Sourced by install.sh

setup_image_deps() {
    # Skip if not installing image features
    if [ "$INSTALL_IMAGE" = "0" ]; then
        return
    fi

    # Intel Arc (unified): torch-XPU 2.12 is already in venv-unified (setup_python_env). Add the
    # image deps to the SAME venv — no separate venv/service. transformers MUST be <5 (SDXL's
    # CLIPTextModel); diffusers renders SDXL ≥768 only with IGC >= 2.35.5 (ensure_igc_235).
    if [ "$BACKEND" = "intel" ] && [ "$IMAGE_BACKEND" = "native" ]; then
        local VENV_NAME="${CHAT_VENV_NAME:-venv-unified}"
        source "$SCRIPT_DIR/$VENV_NAME/bin/activate"
        print_step "Installing native image deps into the unified venv ($VENV_NAME)..."
        pip install "transformers<5" "diffusers>=0.38.0" accelerate safetensors -q
        pip install sentencepiece ftfy -q  # text-to-video (videogeni) T5 tokenizer + prompt cleanup
        pip install onnxruntime huggingface_hub insightface opencv-python-headless -q \
            || print_warning "face-detection deps partially failed (optional)"
        deactivate
        ensure_igc_235
        print_success "Intel unified venv: image generation deps installed"
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

    print_step "Installing native image generation dependencies..."

    # Install diffusers/transformers/accelerate (+ video text-to-video deps)
    pip install diffusers transformers accelerate safetensors -q
    pip install sentencepiece ftfy -q  # text-to-video (videogeni) T5 tokenizer + prompt cleanup
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

# (Removed) setup_xpu_image_instance — superseded by the UNIFIED venv. The Intel chat (llama.cpp
# SYCL) + native image (diffusers torch-XPU) deps live in ONE venv (venv-unified), installed by
# setup_python_env + setup_image_deps. There is no separate venv-xpu / port-3052 image service, and
# its last call site (install.sh "Step 10") is gone with it. The empty stub outlived the thing it
# replaced and read, to anyone grepping, like a service that merely wasn't running.
