#!/bin/bash
# Music generation (ACE-Step) setup module for install.sh.  Run via:  ./install.sh --music
#
# ACE-Step runs as a SEPARATE, persistent systemd service (acestep.service) in its own git checkout,
# because its torch/transformers stack conflicts with the main app, and it ships its own REST server
# (acestep-api). PosterChanAI talks to it over HTTP: each node's app calls its LOCAL acestep
# (music_api_base) after a VRAM swap (prepare_for_music), and load-balances to OTHER nodes via their
# /api/generate-music endpoint. So chat/image/music all queue on one GPU and the GPU is freed before
# each song.
#
# ACE-Step is NOT on PyPI — installed from its git repo with `uv` (which also provisions Python 3.12).
# NVIDIA/CUDA works out of the box. Intel XPU / AMD ROCm need a torch swap + dropping torchcodec
# (CUDA-only) so ACE-Step falls back to soundfile — done automatically below.

setup_music_server() {
    print_banner
    print_step "Setting up the ACE-Step music server (systemd service)..."

    local CLONE_DIR="${ACESTEP_DIR:-$HOME/ACE-Step-1.5}"
    local SVC_USER="${SUDO_USER:-$(whoami)}"

    # --- uv (user-local) -----------------------------------------------------
    if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
        print_step "Installing uv (user-local)..."
        curl -LsSf https://astral.sh/uv/install.sh | sh || { print_error "uv install failed"; return 1; }
    fi
    export PATH="$HOME/.local/bin:$PATH"
    local UV_BIN; UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"

    # --- clone + sync --------------------------------------------------------
    if [ ! -d "$CLONE_DIR" ]; then
        print_step "Cloning ACE-Step into $CLONE_DIR ..."
        git clone https://github.com/ace-step/ACE-Step-1.5.git "$CLONE_DIR" \
            || { print_error "git clone failed"; return 1; }
    fi
    ( cd "$CLONE_DIR" || exit 1
      "$UV_BIN" python install 3.12 || true
      print_step "Resolving ACE-Step dependencies (uv sync)..."
      "$UV_BIN" sync ) || { print_error "uv sync failed — see https://github.com/ace-step/ACE-Step-1.5"; return 1; }

    # --- GPU backend: CUDA works as-is; XPU/ROCm need a torch swap + soundfile fallback ----------
    local GPU_KIND="cuda" SVC_ENV=""
    if command -v nvidia-smi >/dev/null 2>&1; then
        GPU_KIND="cuda"
        SVC_ENV="Environment=PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"
    elif [ -e /dev/dri/renderD128 ] && (lspci 2>/dev/null | grep -qi "Intel.*\(Arc\|Graphics\)"); then
        GPU_KIND="xpu"
        SVC_ENV="Environment=ONEAPI_DEVICE_SELECTOR=level_zero:gpu"
    elif command -v rocminfo >/dev/null 2>&1 || (lspci 2>/dev/null | grep -qi "AMD/ATI"); then
        GPU_KIND="rocm"
    fi
    print_success "Detected music GPU backend: $GPU_KIND"

    if [ "$GPU_KIND" != "cuda" ]; then
        local TORCH_INDEX="${MUSIC_TORCH_INDEX:-https://download.pytorch.org/whl/xpu}"
        [ "$GPU_KIND" = "rocm" ] && TORCH_INDEX="${MUSIC_TORCH_INDEX:-https://download.pytorch.org/whl/rocm6.2}"
        print_step "Swapping torch+vision+audio to $GPU_KIND build (one resolve, matched ABI)..."
        ( cd "$CLONE_DIR" && "$UV_BIN" pip install --reinstall torch torchvision torchaudio --index-url "$TORCH_INDEX" ) \
            || print_warning "torch $GPU_KIND swap had issues — check $TORCH_INDEX"
        # torchcodec is CUDA-only; drop it so ACE-Step uses its soundfile/torchaudio fallback.
        ( cd "$CLONE_DIR" && "$UV_BIN" pip uninstall torchcodec >/dev/null 2>&1 ) || true
        _music_apply_soundfile_patch "$CLONE_DIR"
    fi

    # --- systemd service (persistent; auto-restart) --------------------------
    print_step "Installing acestep.service (systemd)..."
    sudo tee /etc/systemd/system/acestep.service >/dev/null <<UNIT
[Unit]
Description=ACE-Step music server (PosterChanAI musicgeni)
After=network.target

[Service]
Type=simple
User=${SVC_USER}
WorkingDirectory=${CLONE_DIR}
Environment=PATH=${HOME}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin:/opt/cuda/bin
Environment=ACESTEP_API_HOST=0.0.0.0
Environment=ACESTEP_API_PORT=${ACESTEP_PORT:-8001}
${SVC_ENV}
ExecStart=${HOME}/.local/bin/uv run acestep-api
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable --now acestep.service 2>&1 | tail -1

    echo ""
    print_success "Music server installed as acestep.service (port ${ACESTEP_PORT:-8001})."
    echo "  Model auto-downloads on the first song (first one is slower)."
    echo "  In Admin → Music: enable music; set Local Server URL to http://localhost:${ACESTEP_PORT:-8001}"
    echo "  and Remote Music Servers to your OTHER nodes (e.g. http://othernode:3051) to load-balance."
    echo "  Status: systemctl status acestep.service   Logs: journalctl -u acestep.service -f"
}

# Patch ACE-Step's audio save to use soundfile instead of torchaudio.save (which routes through the
# CUDA-only torchcodec on torchaudio>=2.9). Uses the shared scripts/acestep_soundfile_patch.py.
_music_apply_soundfile_patch() {
    local clone="$1"
    print_step "Patching ACE-Step audio save for non-CUDA (torchcodec-free)..."
    python3 "${SCRIPT_DIR}/scripts/acestep_soundfile_patch.py" "$clone" \
        || print_warning "soundfile patch did not apply cleanly (upstream may have changed)"
}
