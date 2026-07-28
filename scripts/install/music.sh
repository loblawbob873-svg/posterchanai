#!/bin/bash
# Music generation (ACE-Step) setup module for install.sh.  Run via:  ./install.sh --music
#
# NOTHING TO INSTALL. Music generation is NATIVE and in-process: `diffusers` ships `AceStepPipeline`,
# so ACE-Step loads on the SAME torch stack as image and video gen (app/services/music_local.py).
#
# This used to clone https://github.com/ace-step/ACE-Step-1.5 into ~/ACE-Step-1.5, install `uv`,
# have uv provision a separate Python 3.12, `uv sync` a conflicting torch stack, register an
# `acestep.service` systemd unit, and then swap torch by hand on Intel XPU / AMD ROCm while dropping
# torchcodec (CUDA-only) so it fell back to soundfile. The app talked to all of that over HTTP.
# None of it is needed now, and none of it is installed here any more.
#
# All this add-on does is PREFETCH the weights so the first `musicgeni` isn't a multi-GB download.
# That is optional — the model downloads on first use either way.

setup_music_server() {
    print_banner
    print_step "Music generation is built in — no separate ACE-Step server to install."

    local VENV_BIN="${CHAT_VENV_NAME:-venv}/bin/python"
    if [ ! -x "$VENV_BIN" ]; then
        print_warning "venv not found ($VENV_BIN) — run the main install first."
        return 1
    fi

    # Confirm the installed diffusers actually carries the pipeline. On an older diffusers the app
    # falls back to an external REST server (music_api_base), so say so rather than failing.
    if ! "$VENV_BIN" -c "from diffusers import AceStepPipeline" >/dev/null 2>&1; then
        print_warning "This diffusers build has no AceStepPipeline — upgrade with:"
        print_warning "    $VENV_BIN -m pip install -U diffusers"
        print_warning "Until then music needs an external acestep server (set music_api_base)."
        return 1
    fi
    print_success "diffusers provides AceStepPipeline (in-process music generation available)"

    # Optional weight prefetch. ~10GB; skip it and the first song just pays the download once.
    local MODEL="${ACESTEP_MODEL:-ACE-Step/Ace-Step1.5}"
    local ans
    read -r -p "Pre-download the music model ($MODEL) now? [y/N]: " ans
    if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
        print_step "Fetching $MODEL (this is large; it is cached in ~/.cache/huggingface)..."
        "$VENV_BIN" - <<PY || print_warning "Prefetch failed — it will download on first use instead."
from huggingface_hub import snapshot_download
snapshot_download("$MODEL")
print("done")
PY
    else
        print_step "Skipping prefetch — the model downloads on the first 'musicgeni'."
    fi

    # A leftover unit from the old split-process setup would keep a GPU-resident model alive for
    # nothing, so retire it rather than leaving it to compete for VRAM.
    if systemctl list-unit-files 2>/dev/null | grep -q '^acestep\.service'; then
        print_warning "Legacy acestep.service found — it is no longer used."
        read -r -p "Stop and disable it? [Y/n]: " ans
        if [ "$ans" != "n" ] && [ "$ans" != "N" ]; then
            sudo systemctl disable --now acestep.service 2>/dev/null \
                && print_success "acestep.service stopped and disabled" \
                || print_warning "Could not disable acestep.service — do it by hand."
        fi
    fi

    print_success "Music generation ready (Admin → Services → Music to enable)."
}
