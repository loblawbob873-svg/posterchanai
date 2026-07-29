#!/bin/bash
# Music generation (ACE-Step) setup module for install.sh.  Run via:  ./install.sh --music
#
# ACE-Step runs IN-PROCESS, on the app's own venv, torch and GPU lock — the same way image and video
# generation do. There is no acestep.service, no second venv, no uv-provisioned Python and no HTTP
# hop. Each node generates locally after a VRAM swap (prepare_for_music) and still load-balances to
# other nodes via their /api/generate-music endpoint.
#
# ACE-Step is NOT on PyPI, so its SOURCE is cloned and installed with `pip install --no-deps -e`.
# --no-deps is load-bearing: its pyproject pins CUDA torch (torch==2.10.0+cu128) plus gradio, and
# letting pip resolve that would replace a hand-built torch-XPU/ROCm install and break image gen on
# the same box. Its real runtime deps (soundfile/loguru/einops/einx/vector-quantize-pytorch) live in
# requirements.txt; only torchaudio is resolved here, because it must match the installed torch build.
#
# History worth keeping: an earlier attempt at this loaded ACE-Step through diffusers'
# AceStepPipeline and retired the service on that basis. from_pretrained looks for a
# model_index.json that NO published ACE-Step repo carries, so it 404'd and the service had to be
# restored. The weights load fine through upstream's OWN AceStepHandler — the same code the service
# was running — which is what this installs.

# Resolve the app venv. NOT a hardcoded `venv-unified`: the nodes disagree — server1 (Arc) has
# `venv-unified/`, nas has plain `venv/`, and install.sh's own steps use `venv/`. Hardcoding either
# one made `./install.sh --music` abort with "App venv not found" on the other node, i.e. half the
# fleet could not install music at all. Honour VENV_DIR, else take whichever exists.
_music_find_venv() {
    if [ -n "${VENV_DIR:-}" ]; then echo "$VENV_DIR"; return; fi
    for c in "$SCRIPT_DIR/venv-unified" "$SCRIPT_DIR/venv"; do
        [ -x "$c/bin/python" ] && { echo "$c"; return; }
    done
    echo "$SCRIPT_DIR/venv-unified"   # nothing found: report the conventional path in the error
}

setup_music_server() {
    print_banner
    print_step "Setting up in-process music generation (ACE-Step)..."

    local CLONE_DIR="${ACESTEP_DIR:-$HOME/ACE-Step-1.5}"
    local VENV; VENV="$(_music_find_venv)"
    local PY="$VENV/bin/python"

    if [ ! -x "$PY" ]; then
        print_error "App venv not found at $VENV — run the main install first."
        return 1
    fi

    if [ ! -d "$CLONE_DIR/.git" ]; then
        print_step "Cloning ACE-Step 1.5 into $CLONE_DIR ..."
        git clone https://github.com/ace-step/ACE-Step-1.5.git "$CLONE_DIR" \
            || { print_error "git clone failed"; return 1; }
    else
        print_step "ACE-Step already cloned at $CLONE_DIR"
    fi

    # torchaudio MUST come from the same index as the installed torch, or pip fetches a CPU build and
    # drags a CPU torch in behind it. Derive the index from what torch actually reports.
    local TORCH_VER TORCH_IDX
    TORCH_VER="$("$PY" -c 'import torch;print(torch.__version__)' 2>/dev/null || echo "")"
    case "$TORCH_VER" in
        *xpu*)  TORCH_IDX="https://download.pytorch.org/whl/xpu" ;;
        *rocm*) TORCH_IDX="https://download.pytorch.org/whl/rocm6.3" ;;
        *cu12*) TORCH_IDX="https://download.pytorch.org/whl/cu121" ;;
        *)      TORCH_IDX="" ;;
    esac
    if "$PY" -c 'import torchaudio' 2>/dev/null; then
        print_step "torchaudio already present"
    elif [ -n "$TORCH_IDX" ]; then
        print_step "Installing torchaudio from $TORCH_IDX (matching torch $TORCH_VER)..."
        "$VENV/bin/pip" install -q --no-deps torchaudio --index-url "$TORCH_IDX" \
            || print_warning "torchaudio install failed — music will not load until it is present"
    else
        "$VENV/bin/pip" install -q --no-deps torchaudio || print_warning "torchaudio install failed"
    fi

    print_step "Installing ACE-Step into the app venv (--no-deps)..."
    "$VENV/bin/pip" install -q --no-deps -e "$CLONE_DIR" \
        || { print_error "ACE-Step install failed"; return 1; }

    # Guard the one thing that would wreck the box: a resolver swapping torch out underneath us.
    local TORCH_AFTER; TORCH_AFTER="$("$PY" -c 'import torch;print(torch.__version__)' 2>/dev/null || echo "")"
    if [ -n "$TORCH_VER" ] && [ "$TORCH_VER" != "$TORCH_AFTER" ]; then
        print_error "torch changed during install ($TORCH_VER -> $TORCH_AFTER) — reinstall torch for your GPU!"
    fi

    print_step "Verifying ACE-Step imports from the app venv..."
    if "$PY" -c 'from acestep.handler import AceStepHandler' 2>/dev/null; then
        print_success "ACE-Step imports"
    else
        print_error "ACE-Step does not import — check its runtime deps in requirements.txt"
        return 1
    fi

    _music_retire_sidecar

    print_success "Music generation ready (in-process — no systemd service)."
    echo ""
    echo "  Models download on first use into $CLONE_DIR/checkpoints (several GB)."
    echo "  In Admin → Music: enable music. Leave the Local Server URL EMPTY —"
    echo "  setting it forces the old HTTP path instead of generating in-process."
    echo ""
}

# Remove a sidecar left over from the old layout. Harmless when it was never installed; without it
# the retired service keeps a GPU-resident model and port 8001 for nothing.
_music_retire_sidecar() {
    if systemctl list-unit-files 2>/dev/null | grep -q '^acestep\.service'; then
        print_step "Retiring the old acestep.service (music is in-process now)..."
        sudo systemctl disable --now acestep.service 2>/dev/null
        sudo rm -f /etc/systemd/system/acestep.service /etc/systemd/system/acestep.service.retired
        sudo systemctl daemon-reload
        print_success "acestep.service removed"
    fi
}

# Update an existing ACE-Step checkout (installer option 6 / run_updates). No-op if not installed.
# Still --no-deps, so an upstream pyproject change can never pull CUDA torch over the GPU build.
update_music_server() {
    local CLONE_DIR="${ACESTEP_DIR:-$HOME/ACE-Step-1.5}"
    local VENV; VENV="$(_music_find_venv)"
    if [ ! -d "$CLONE_DIR/.git" ]; then
        return 0  # ACE-Step not installed here
    fi
    print_step "Updating ACE-Step (git pull)..."
    git -C "$CLONE_DIR" pull --ff-only 2>&1 | tail -2
    "$VENV/bin/pip" install -q --no-deps -e "$CLONE_DIR" 2>/dev/null \
        && echo "  ✓ ACE-Step updated (restart posterchanai to pick it up)" \
        || print_warning "ACE-Step re-install had issues"
    _music_retire_sidecar
}
