#!/bin/bash
# Image editing (regeni) setup module for install.sh.  Run via:  ./install.sh --regeni
#
# regeni = text-grounded auto-mask (CLIPSeg) + SDXL inpaint. It runs NATIVELY in-process on the SAME
# diffusers/torch stack as image generation and REUSES the geni SDXL checkpoints (image_model_path /
# image_anime_model_path) — there's no separate edit model. So there are NO new pip deps: CLIPSeg is
# a `transformers` model and inpaint is diffusers' StableDiffusionXLInpaintPipeline, both already in
# the image venv. We just make sure the stack is present and optionally prefetch the small (~1.2GB)
# CLIPSeg model. Editing shares the app's GPU lock + VRAM swap and load-balances via /api/edit-image.
#
# Why this design: SDXL + CLIPSeg use small CLIP text encoders (not a 7-8B LLM encoder), so the whole
# thing is ~8GB and is PORTABLE across CUDA, Intel XPU and AMD ROCm — it fits a 16GB Arc / 12GB card
# with no offload, where every big editor (Qwen-Edit/Flux.2/LongCat) does not.
#
# Portability: stock diffusers/transformers + torch SDPA only — NO flash-attn/fp8/GGUF.

# Pick the venv that holds the diffusers/image stack (intel = venv-unified; otherwise venv).
_regeni_pick_venv() {
    local c
    for c in "${CHAT_VENV_NAME:-}" "venv-unified" "venv" "venv-xpu-new" "venv-xpu"; do
        [ -n "$c" ] && [ -x "$SCRIPT_DIR/$c/bin/python" ] && { echo "$c"; return 0; }
    done
    return 1
}

setup_regeni_deps() {
    print_step "Setting up image editing (regeni)..."
    local VENV_NAME
    VENV_NAME="$(_regeni_pick_venv)" || { print_warning "No image/diffusers venv found — run the image-generation install first, then ./install.sh --regeni"; return 1; }
    print_step "Using venv: $VENV_NAME"
    source "$SCRIPT_DIR/$VENV_NAME/bin/activate"

    # The diffusers/transformers stack (shared with native image gen). CLIPSeg + SDXL inpaint need no
    # extra packages — just ensure the stack is present.
    pip install "transformers<5" "diffusers>=0.38.0" accelerate safetensors -q \
        || print_warning "diffusers stack install had issues"
    print_success "Image-edit deps present (CLIPSeg + SDXL inpaint — no extra packages)"

    # regeni reuses your geni SDXL models — warn if none is configured yet.
    if [ -d "$SCRIPT_DIR/models" ] && ! ls "$SCRIPT_DIR/models"/*.safetensors >/dev/null 2>&1; then
        print_warning "No SDXL model found in ./models — regeni reuses the geni Image Model (set it in Admin -> Image)."
    fi

    # Prefetch the small CLIPSeg segmentation model (~1.2GB) so the first regeni isn't a download.
    local DL="${REGENI_PREFETCH:-}"
    if [ -z "$DL" ]; then
        read -p "Pre-download the CLIPSeg mask model now (~1.2GB)? It downloads on first regeni otherwise. [Y/n]: " DL
    fi
    if [[ ! "$DL" =~ ^[Nn] ]]; then
        print_step "Downloading CIDAS/clipseg-rd64-refined ..."
        python -c "from huggingface_hub import snapshot_download; snapshot_download('CIDAS/clipseg-rd64-refined')" \
            && print_success "CLIPSeg cached" \
            || print_warning "CLIPSeg prefetch failed — it will download on first use instead"
    fi
    deactivate
    print_success "Image editing ready — enable it in Admin -> Image and use 'regeni <instruction>' (e.g. 'change the background to a beach')"
    return 0
}
