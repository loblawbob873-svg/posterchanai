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
    local GPU_KIND="cuda"
    if command -v nvidia-smi >/dev/null 2>&1; then
        GPU_KIND="cuda"
    elif [ -e /dev/dri/renderD128 ] && (lspci 2>/dev/null | grep -qi "Intel.*\(Arc\|Graphics\)"); then
        GPU_KIND="xpu"
    elif command -v rocminfo >/dev/null 2>&1 || (lspci 2>/dev/null | grep -qi "AMD/ATI"); then
        GPU_KIND="rocm"
    fi
    print_success "Detected music GPU backend: $GPU_KIND"

    if [ "$GPU_KIND" != "cuda" ]; then
        local TORCH_INDEX="${MUSIC_TORCH_INDEX:-https://download.pytorch.org/whl/xpu}"
        # ROCm 6.3+ ships torch>=2.7 (rocm6.2 tops out at 2.5.1, too old for ACE-Step — it needs
        # torch.int1 from 2.6+). Override with MUSIC_TORCH_INDEX for a different ROCm runtime.
        [ "$GPU_KIND" = "rocm" ] && TORCH_INDEX="${MUSIC_TORCH_INDEX:-https://download.pytorch.org/whl/rocm6.3}"
        print_step "Swapping torch+vision+audio to $GPU_KIND build (one resolve, matched ABI)..."
        ( cd "$CLONE_DIR" && "$UV_BIN" pip install --reinstall torch torchvision torchaudio --index-url "$TORCH_INDEX" ) \
            || print_warning "torch $GPU_KIND swap had issues — check $TORCH_INDEX"
        # torchcodec is CUDA-only; drop it so ACE-Step uses its soundfile/torchaudio fallback.
        ( cd "$CLONE_DIR" && "$UV_BIN" pip uninstall torchcodec >/dev/null 2>&1 ) || true
        _music_apply_soundfile_patch "$CLONE_DIR"
    fi

    # Per-GPU service environment. NOTE: we run .venv/bin/acestep-api DIRECTLY (not `uv run`) —
    # `uv run` re-syncs to uv.lock (CUDA torch) on every launch and would silently revert the
    # XPU/ROCm torch swap back to CUDA → CPU fallback (no GPU activity).
    local PYVER; PYVER="$("$CLONE_DIR/.venv/bin/python" -c 'import sys;print(f"python{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo python3.12)"
    local TORCH_LIB="${CLONE_DIR}/.venv/lib/${PYVER}/site-packages/torch/lib"
    local SVC_ENV=""
    case "$GPU_KIND" in
        cuda) SVC_ENV="Environment=PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True" ;;
        xpu)  SVC_ENV="Environment=ONEAPI_DEVICE_SELECTOR=level_zero:gpu
Environment=ZES_ENABLE_SYSMAN=1
Environment=SYCL_CACHE_PERSISTENT=1
Environment=ACESTEP_DEVICE=xpu
Environment=LD_LIBRARY_PATH=${TORCH_LIB}:/usr/lib64" ;;
        rocm) SVC_ENV="Environment=LD_LIBRARY_PATH=${TORCH_LIB}:/usr/lib64" ;;
    esac

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
Environment=PATH=${CLONE_DIR}/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin:/opt/cuda/bin
Environment=ACESTEP_API_HOST=0.0.0.0
Environment=ACESTEP_API_PORT=${ACESTEP_PORT:-8001}
${SVC_ENV}
ExecStart=${CLONE_DIR}/.venv/bin/acestep-api
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

# Update an existing ACE-Step install (installer option 6 / run_updates): refresh the code
# (git pull) + deps (uv sync), re-apply the non-CUDA torch swap + soundfile patch (uv sync reverts
# torch to the locked CUDA build), then restart the service. Models are versioned by name and
# auto-(re)download on demand, so a component update doesn't need a separate model fetch. No-op if
# ACE-Step isn't installed on this host.
update_music_server() {
    local CLONE_DIR="${ACESTEP_DIR:-$HOME/ACE-Step-1.5}"
    if [ ! -d "$CLONE_DIR" ] || ! systemctl list-unit-files 2>/dev/null | grep -q '^acestep\.service'; then
        return 0  # ACE-Step not installed here
    fi
    print_step "Updating ACE-Step music server (git pull + uv sync)..."
    export PATH="$HOME/.local/bin:$PATH"
    local UV_BIN; UV_BIN="$(command -v uv || echo "$HOME/.local/bin/uv")"
    ( cd "$CLONE_DIR" && git pull --ff-only 2>&1 | tail -2 && "$UV_BIN" sync ) \
        || { print_warning "ACE-Step update (git/uv sync) had issues"; return 1; }

    # Re-detect GPU; uv sync reset torch to the CUDA lock, so re-swap for XPU/ROCm.
    local GPU_KIND="cuda"
    if command -v nvidia-smi >/dev/null 2>&1; then GPU_KIND="cuda"
    elif [ -e /dev/dri/renderD128 ] && (lspci 2>/dev/null | grep -qi "Intel.*\(Arc\|Graphics\)"); then GPU_KIND="xpu"
    elif command -v rocminfo >/dev/null 2>&1 || (lspci 2>/dev/null | grep -qi "AMD/ATI"); then GPU_KIND="rocm"; fi
    if [ "$GPU_KIND" != "cuda" ]; then
        local TORCH_INDEX="${MUSIC_TORCH_INDEX:-https://download.pytorch.org/whl/xpu}"
        [ "$GPU_KIND" = "rocm" ] && TORCH_INDEX="${MUSIC_TORCH_INDEX:-https://download.pytorch.org/whl/rocm6.3}"
        ( cd "$CLONE_DIR" && "$UV_BIN" pip install --reinstall torch torchvision torchaudio --index-url "$TORCH_INDEX" \
            && ( "$UV_BIN" pip uninstall torchcodec >/dev/null 2>&1 || true ) ) \
            || print_warning "torch $GPU_KIND re-swap had issues"
        _music_apply_soundfile_patch "$CLONE_DIR"
    fi

    sudo systemctl restart acestep.service 2>/dev/null && echo "  ✓ acestep.service restarted (updated)" \
        || print_warning "Could not restart acestep.service"
}
