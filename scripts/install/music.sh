#!/bin/bash
# Music generation (ACE-Step) setup module for install.sh.  Run via:  ./install.sh --music
#
# ACE-Step runs as a SEPARATE service in its own checkout because its torch/transformers stack
# conflicts with the main app, and it ships its own REST server (acestep-api). PosterChanAI talks
# to it over HTTP (app/services/music_service.py), so the main venv gains nothing heavy.
#
# ACE-Step is NOT on PyPI — it's installed from its git repo with `uv` (its own recommended
# installer), which also provisions Python 3.12 and resolves the locked deps (incl. a CUDA torch
# wheel). The DiT model auto-downloads on first generation (the first song is slower).

setup_music_server() {
    print_banner
    print_step "Setting up the ACE-Step music server (uv + git clone)..."

    local CLONE_DIR="${ACESTEP_DIR:-$HOME/ACE-Step-1.5}"

    # uv (user-local, no root). Provisions Python 3.12 and resolves ACE-Step's locked deps.
    if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
        print_step "Installing uv (user-local)..."
        curl -LsSf https://astral.sh/uv/install.sh | sh || { print_error "uv install failed"; return 1; }
    fi
    export PATH="$HOME/.local/bin:$PATH"
    uv --version || { print_error "uv not on PATH after install"; return 1; }

    if [ ! -d "$CLONE_DIR" ]; then
        print_step "Cloning ACE-Step into $CLONE_DIR ..."
        git clone https://github.com/ace-step/ACE-Step-1.5.git "$CLONE_DIR" \
            || { print_error "git clone failed"; return 1; }
    else
        print_success "ACE-Step checkout exists: $CLONE_DIR"
    fi

    ( cd "$CLONE_DIR" || exit 1
      uv python install 3.12 || true
      print_step "Resolving ACE-Step dependencies (uv sync — installs CUDA torch + deps)..."
      uv sync ) || { print_error "uv sync failed — see https://github.com/ace-step/ACE-Step-1.5"; return 1; }

    print_success "ACE-Step ready"
    echo ""
    print_success "Start the music server with:"
    echo "    cd $CLONE_DIR && ACESTEP_API_HOST=0.0.0.0 ACESTEP_API_PORT=8001 uv run acestep-api"
    echo ""
    echo "  For AMD ROCm or Intel XPU, swap torch after sync, e.g.:"
    echo "    cd $CLONE_DIR && uv pip install --reinstall torch torchvision torchaudio \\"
    echo "        --index-url https://download.pytorch.org/whl/rocm6.2   # (or .../xpu)"
    echo "    (NVIDIA/CUDA works out of the box; ROCm/XPU may need matching torchvision/torchaudio.)"
    echo ""
    echo "  Then in Admin → Music: enable music and set the Local Server URL to http://localhost:8001"
    echo "  (the DiT model auto-downloads on the first song)."
}
