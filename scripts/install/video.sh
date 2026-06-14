#!/bin/bash
# Video generation (videogeni) setup module for install.sh.  Run via:  ./install.sh --video
#
# Unlike music (ACE-Step, a separate service), text-to-video runs NATIVELY in-process on the SAME
# diffusers/torch stack as image generation — Wan2.1 / LTX / CogVideoX are stock `diffusers`
# pipelines. So there's no extra venv or service: we just add the two missing deps to the image
# venv (sentencepiece — REQUIRED for the T5 text-encoder tokenizer — and ftfy), make sure the
# diffusers stack is present, and optionally pre-fetch the model. Generation shares the app's GPU
# lock + VRAM swap (prepare_for_video) and load-balances to other nodes via /api/generate-video.
#
# Portability: stay on stock diffusers + torch SDPA so it runs on CUDA, Intel Arc (XPU) and AMD
# (ROCm). NO flash-attn/xformers/fp8/GGUF (CUDA-pinned). The Arc 16GB is memory-tight — keep
# frames/resolution modest (Admin -> Video).

# Pick the venv that holds the diffusers/image stack (intel = venv-unified; otherwise venv).
_video_pick_venv() {
    local c
    for c in "${CHAT_VENV_NAME:-}" "venv-unified" "venv" "venv-xpu-new" "venv-xpu"; do
        [ -n "$c" ] && [ -x "$SCRIPT_DIR/$c/bin/python" ] && { echo "$c"; return 0; }
    done
    return 1
}

setup_video_deps() {
    print_step "Installing video generation (videogeni) dependencies..."
    local VENV_NAME
    VENV_NAME="$(_video_pick_venv)" || { print_warning "No image/diffusers venv found — run the image-generation install first, then ./install.sh --video"; return 1; }
    print_step "Using venv: $VENV_NAME"
    source "$SCRIPT_DIR/$VENV_NAME/bin/activate"

    # The diffusers stack (shared with native image gen) + the two video-only deps. transformers<5
    # for SDXL compatibility; sentencepiece is mandatory for the T5/umT5 video text-encoder.
    pip install "transformers<5" "diffusers>=0.38.0" accelerate safetensors -q \
        || print_warning "diffusers stack install had issues"
    pip install sentencepiece ftfy -q \
        || { print_error "sentencepiece/ftfy install failed — videogeni's text encoder won't load"; deactivate; return 1; }
    print_success "Video deps installed (diffusers + sentencepiece + ftfy)"

    # Optional: pre-fetch the model (~27GB) so the first videogeni isn't a long download. Otherwise
    # diffusers auto-downloads it to HF_HOME on first use (like the image model).
    local MODEL="${VIDEO_MODEL:-Wan-AI/Wan2.1-T2V-1.3B-Diffusers}"
    local DL_VIDEO="${VIDEO_PREFETCH:-}"
    if [ -z "$DL_VIDEO" ]; then
        read -p "Pre-download the video model now ($MODEL, ~27GB)? It downloads on first videogeni otherwise. [y/N]: " DL_VIDEO
    fi
    if [[ "$DL_VIDEO" =~ ^[Yy] ]]; then
        print_step "Downloading $MODEL into the Hugging Face cache (this is large)..."
        python -c "from huggingface_hub import snapshot_download; snapshot_download('$MODEL')" \
            && print_success "Video model cached" \
            || print_warning "Model prefetch failed — it will download on first use instead"
    fi
    deactivate
    print_success "Video generation ready — enable it in Admin -> Video and use 'videogeni <prompt>'"
    return 0
}
