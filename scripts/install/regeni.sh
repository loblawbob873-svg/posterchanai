#!/bin/bash
# Image editing (regeni) setup module for install.sh.  Run via:  ./install.sh --regeni
#
# Like videogeni, instruction editing runs NATIVELY in-process on the SAME diffusers/torch stack as
# image generation — OmniGen v1 is a stock `diffusers` pipeline (OmniGenPipeline, shipped in
# diffusers>=0.38.0). So there's no extra venv or service and NO new deps beyond the image stack
# (transformers + sentencepiece, already required for video). We just ensure the stack is present
# and optionally pre-fetch the ~9GB model. Editing shares the app's GPU lock + VRAM swap
# (prepare_for_imageedit) and load-balances to other nodes via /api/edit-image.
#
# Why OmniGen v1 (not a SOTA editor): the good editors (LongCat/OmniGen2/Qwen-Edit) ship a ~16GB
# Qwen2.5-VL text encoder that fits neither a 16GB Arc (offload is broken on XPU) nor a 12GB card.
# OmniGen v1 has no separate large encoder — ~9GB total, fits both, runs on CUDA + Intel XPU.
#
# Portability: stock diffusers + torch SDPA only — NO flash-attn/xformers/fp8/GGUF (CUDA-pinned).

# Pick the venv that holds the diffusers/image stack (intel = venv-unified; otherwise venv).
_regeni_pick_venv() {
    local c
    for c in "${CHAT_VENV_NAME:-}" "venv-unified" "venv" "venv-xpu-new" "venv-xpu"; do
        [ -n "$c" ] && [ -x "$SCRIPT_DIR/$c/bin/python" ] && { echo "$c"; return 0; }
    done
    return 1
}

setup_regeni_deps() {
    print_step "Installing image editing (regeni) dependencies..."
    local VENV_NAME
    VENV_NAME="$(_regeni_pick_venv)" || { print_warning "No image/diffusers venv found — run the image-generation install first, then ./install.sh --regeni"; return 1; }
    print_step "Using venv: $VENV_NAME"
    source "$SCRIPT_DIR/$VENV_NAME/bin/activate"

    # The diffusers stack (shared with native image gen). diffusers>=0.38.0 ships OmniGenPipeline;
    # transformers<5 for SDXL compatibility; sentencepiece for the Phi-3 tokenizer. No regeni-only deps.
    pip install "transformers<5" "diffusers>=0.38.0" accelerate safetensors sentencepiece -q \
        || print_warning "diffusers stack install had issues"
    print_success "Image-edit deps present (diffusers ships OmniGenPipeline — no extra deps)"

    # Optional: pre-fetch the model (~9GB) so the first regeni isn't a long download. Otherwise
    # diffusers auto-downloads it to HF_HOME on first use (like the image model).
    local MODEL="${REGENI_MODEL:-Shitao/OmniGen-v1-diffusers}"
    local DL_REGENI="${REGENI_PREFETCH:-}"
    if [ -z "$DL_REGENI" ]; then
        read -p "Pre-download the edit model now ($MODEL, ~9GB)? It downloads on first regeni otherwise. [y/N]: " DL_REGENI
    fi
    if [[ "$DL_REGENI" =~ ^[Yy] ]]; then
        print_step "Downloading $MODEL into the Hugging Face cache..."
        python -c "from huggingface_hub import snapshot_download; snapshot_download('$MODEL')" \
            && print_success "Edit model cached" \
            || print_warning "Model prefetch failed — it will download on first use instead"
    fi
    deactivate
    print_success "Image editing ready — enable it in Admin -> Image and use 'regeni <instruction>'"
    return 0
}
