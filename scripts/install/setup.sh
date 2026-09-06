#!/bin/bash
# Setup Functions - Directories, Python environment, database
# Sourced by install.sh

setup_directories() {
    print_step "Setting up directories..."

    # Create upload directory
    local UPLOAD_PATH="/var/lib/posterchanai"
    if [ ! -d "$UPLOAD_PATH" ]; then
        sudo mkdir -p "$UPLOAD_PATH"
        sudo chown "$(whoami)":"$(whoami)" "$UPLOAD_PATH"
        print_success "Created $UPLOAD_PATH"
    else
        print_success "Upload directory exists"
    fi

    # Create models directory
    local MODELS_PATH="$UPLOAD_PATH/models"
    if [ ! -d "$MODELS_PATH" ]; then
        sudo mkdir -p "$MODELS_PATH"
        sudo chown "$(whoami)":"$(whoami)" "$MODELS_PATH"
        print_success "Created $MODELS_PATH"
    fi

    # Create data directory
    local DATA_PATH="$SCRIPT_DIR/data"
    if [ ! -d "$DATA_PATH" ]; then
        mkdir -p "$DATA_PATH"
        print_success "Created $DATA_PATH"
    fi

    # Media Center source directory is durable; only derived transcodes use /tmp.
    local MEDIA_PATH="${POSTERCHANAI_DATA:-$UPLOAD_PATH}/media"
    if [ ! -d "$MEDIA_PATH" ]; then
        sudo mkdir -p -m 750 "$MEDIA_PATH"
        sudo chown "$(whoami)":"$(whoami)" "$MEDIA_PATH"
    fi
    if [ ! -e "$SCRIPT_DIR/media-center.env" ]; then
        install -m 600 "$SCRIPT_DIR/media-center.env.example" "$SCRIPT_DIR/media-center.env"
    fi
    print_success "Media Center: $MEDIA_PATH (override roots in media-center.env)"
    echo "  Configure the NAS proxy in Admin -> Storage; grant Media Center in Additional permissions."
    echo "  Transcodes use /tmp/posterchan-media-center; mount /tmp as tmpfs to avoid SSD writes."
}

setup_python_env() {
    print_step "Setting up Python environment..."

    # Intel Arc uses ONE unified venv (venv-unified) for BOTH chat (llama.cpp SYCL) and
    # native image gen (diffusers torch-XPU). Others use venv. (The old split — venv-ipex
    # IPEX-LLM chat + venv-xpu-new image — is gone; IPEX-LLM is EOL.)
    local VENV_NAME="venv"
    if [ "$BACKEND" = "intel" ]; then
        VENV_NAME="venv-unified"
    fi

    # Export for use by other modules
    export CHAT_VENV_NAME="$VENV_NAME"

    # Pin the venv to Python 3.13 when available. The main app + image stack (torch-XPU 2.12 /
    # CUDA torch 2.5+) all support 3.13, and the prebuilt `libtorrent` wheel is cp313 — so building
    # the venv with a bare `python3` is risky on nodes whose system default has drifted (e.g.
    # nas.lan went to 3.14, where the cp313 libtorrent wheel won't load). Prefer 3.13, then 3.12,
    # then whatever `python3` is.
    local PYBIN="python3"
    for cand in python3.13 python3.12; do
        if command -v "$cand" >/dev/null 2>&1; then PYBIN="$cand"; break; fi
    done

    # Several deps (coincurve, libtorrent, torch-XPU) ship prebuilt wheels only up to cp313.
    # On Python 3.14+ pip is forced to build from source — coincurve then dies on the cffi
    # "Expected exactly one LICENSE file" build bug. A bare `python3` that has drifted to 3.14
    # (e.g. nas.lan) must NOT be used; if 3.13/3.12 wasn't found above, refuse rather than ship
    # a broken tree.
    if ! "$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info[:2] <= (3, 13) else 1)' 2>/dev/null; then
        print_error "No supported Python found (need 3.13 or 3.12; $($PYBIN --version 2>&1) lacks prebuilt wheels for coincurve/libtorrent/torch-XPU)."
        print_error "Install python3.13 (or 3.12) and re-run, e.g.:  sudo emerge -av dev-lang/python:3.13   # or your distro's python3.13 package"
        return 1
    fi

    # If a venv already exists, make sure it was built with a supported Python — a stale 3.14
    # venv reused here is the actual cause of the coincurve/cffi build failure. Recreate it.
    if [ -d "$VENV_NAME" ] && [ -x "$VENV_NAME/bin/python" ]; then
        if ! "$VENV_NAME/bin/python" -c 'import sys; sys.exit(0 if sys.version_info[:2] <= (3, 13) else 1)' 2>/dev/null; then
            print_warning "Existing $VENV_NAME uses $($VENV_NAME/bin/python --version 2>&1) (unsupported — no prebuilt wheels). Recreating with $($PYBIN --version 2>&1)."
            rm -rf "$VENV_NAME"
        fi
    fi

    if [ ! -d "$VENV_NAME" ]; then
        "$PYBIN" -m venv "$VENV_NAME"
        print_success "Created virtual environment: $VENV_NAME ($($PYBIN --version 2>&1))"
    else
        print_success "Virtual environment exists: $VENV_NAME ($($VENV_NAME/bin/python --version 2>&1))"
    fi

    source "$VENV_NAME/bin/activate"
    pip install --upgrade pip -q

    # Intel: install native PyTorch-XPU FIRST (it bundles its own oneAPI runtime), so the
    # requirements.txt step finds torch satisfied and doesn't pull a CPU build over it. Modern
    # stack — no IPEX (EOL after 2.8), no numpy<2 pin (torch 2.12 ships with numpy 2).
    if [ "$BACKEND" = "intel" ]; then
        print_step "Installing PyTorch 2.12 XPU (native, bundles oneAPI runtime)..."
        pip install torch==2.12.0 torchvision --index-url https://download.pytorch.org/whl/xpu -q \
            || pip install torch==2.12.0 --index-url https://download.pytorch.org/whl/xpu -q
    fi

    print_step "Installing Python dependencies..."
    if [ "${NOSTR_ONLY:-0}" = "1" ] && [ -f requirements-nostr.txt ]; then
        # Nostr-only: lean web + relay + Blossom + non-AI deps (no torch/llama/diffusers).
        print_step "Nostr-only mode → installing requirements-nostr.txt (no AI stack)"
        pip install -r requirements-nostr.txt -q
    else
        pip install -r requirements.txt -q
    fi

    # Merged bot framework (botframework/) has its own deps (psycopg2, edge-tts, pytz, …)
    # used by the bots spawned via Admin → Bots. Install them into the same chat venv since
    # bot_manager_service runs the bots with this interpreter.
    if [ -f botframework/requirements.txt ]; then
        pip install -r botframework/requirements.txt -q || print_warning "Some botframework deps failed to install"
    fi

    print_success "Base dependencies installed"

    # Optionally pre-download the Whisper voice model. It downloads automatically
    # on first voice use anyway, so this is just a head start — and pointless for
    # lean/Telegram-only installs, so skip the prompt there.
    if pip show faster-whisper > /dev/null 2>&1; then
        if [ "${INSTALL_LLM:-0}" = "0" ] && [ "${INSTALL_IMAGE:-0}" = "0" ]; then
            echo "  Skipping Whisper voice-model pre-download (lean install; it downloads on first use if needed)."
        else
            read -p "Pre-download the Whisper voice model now (~1.5GB)? It downloads on first voice use otherwise. [y/N]: " DL_WHISPER
            if [[ "$DL_WHISPER" =~ ^[Yy] ]]; then
                print_step "Downloading Whisper speech recognition model (~1.5GB)..."
                if python -c "
from faster_whisper import WhisperModel
import sys
print('Downloading Whisper medium model...', file=sys.stderr)
model = WhisperModel('medium', device='cpu', compute_type='int8')
print('Whisper model ready', file=sys.stderr)
" 2>&1; then
                    print_success "Whisper model downloaded"
                else
                    print_warning "Whisper download failed (voice input may be slower on first use)"
                fi
            fi
        fi
    fi

    deactivate
}

configure_database_settings() {
    print_step "Configuring database settings..."

    # The local LLM backend is always native llama.cpp now (no llm_backend/image_backend
    # settings). Initialize the database if needed.
    if [ ! -f "posterchanai.db" ]; then
        print_step "Initializing database..."
        local VENV_PATH="venv"
        [ "$BACKEND" = "intel" ] && VENV_PATH="venv-unified"
        "$VENV_PATH/bin/python" -c "from app.database import init_db; init_db()" 2>/dev/null || true
    fi

    # Intel unified stack: native image gen in a per-gen subprocess so it releases VRAM
    # back to the resident LLM on the shared GPU.
    if [ -f "posterchanai.db" ] && [ "$BACKEND" = "intel" ] && [ "$IMAGE_BACKEND" = "native" ]; then
        sqlite3 posterchanai.db "INSERT OR REPLACE INTO settings (key, value) VALUES ('image_gpu_device', 'xpu');" 2>/dev/null
        sqlite3 posterchanai.db "INSERT OR REPLACE INTO settings (key, value) VALUES ('image_subprocess_mode', 'true');" 2>/dev/null
        print_success "Intel: native image gen + subprocess VRAM release enabled"
    fi

    # Point the agentic/tools model (llm_tools_model) at the best coding GGUF we downloaded, by
    # FULL PATH (prefer the 30B Coder, then the lightweight Claude-Code). Blank → falls back to the
    # main model, so this only sets it when a real coding model is present.
    if [ -f "posterchanai.db" ]; then
        local MODELS_PATH="${UPLOAD_PATH:-/var/lib/posterchanai}/models"
        for cm in "Qwen3-Coder-30B-A3B-Instruct-IQ4_XS.gguf" "Qwen3.5-9B-Claude-Code-Q4_K_M.gguf"; do
            if [ -f "$MODELS_PATH/$cm" ]; then
                sqlite3 posterchanai.db "INSERT OR REPLACE INTO settings (key, value) VALUES ('llm_tools_model', '$MODELS_PATH/$cm');" 2>/dev/null
                print_success "Agentic/tools model set to $cm"
                break
            fi
        done
    fi
}

# Shared file downloader (wget|curl) - single implementation so the multi-model path below
# isn't duplicated. $1=url $2=dest; returns 0 on success, leaving no partial file behind.
_download_model_file() {
    local url="$1" dest="$2" ok=0
    echo "  -> $(basename "$dest")"
    if command -v wget &>/dev/null; then
        wget -q --show-progress -O "$dest" "$url" && ok=1
    elif command -v curl &>/dev/null; then
        curl -L --progress-bar -o "$dest" "$url" && ok=1
    else
        print_warning "Neither wget nor curl found. Download manually:"
        echo "    $url"; echo "    -> $dest"; return 1
    fi
    if [ "$ok" = "1" ] && [ -s "$dest" ]; then
        print_success "Downloaded $(basename "$dest")"; return 0
    fi
    [ -f "$dest" ] && rm -f "$dest"
    print_error "Download failed: $url"; return 1
}

# Depth-Anything V2 (small ViT-S) ONNX — powers the `alive` 3D-parallax effect.
# Gitignored (~94 MB) so it isn't in the repo; fetch it into the checkout's assets/
# dir (a path parallax_service.py looks in). Idempotent: skips if already present.
DEPTH_MODEL_URL="https://huggingface.co/onnx-community/depth-anything-v2-small/resolve/main/onnx/model.onnx"

download_depth_model() {
    local dest="$SCRIPT_DIR/assets/depth_anything_v2_vits.onnx"
    if [ -s "$dest" ]; then
        print_success "Depth model already present (alive/parallax effect)"
        return 0
    fi
    print_step "Downloading depth model for the 'alive' 3D effect (~94MB)..."
    mkdir -p "$SCRIPT_DIR/assets"
    if _download_model_file "$DEPTH_MODEL_URL" "$dest"; then
        print_success "Depth model ready"
    else
        print_warning "Depth model download failed — the 'alive' command stays disabled until it's present at $dest"
    fi
}

# u2net ONNX — powers the `removebackground` command (rembg). rembg fetches this on first use
# into ~/.u2net/; pre-fetch it here so the first removebackground doesn't stall. Idempotent.
U2NET_MODEL_URL="https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx"

download_u2net_model() {
    local dest="${U2NET_HOME:-$HOME/.u2net}/u2net.onnx"
    if [ -s "$dest" ]; then
        print_success "Background-removal model already present (removebackground)"
        return 0
    fi
    print_step "Downloading background-removal model for 'removebackground' (~176MB)..."
    mkdir -p "$(dirname "$dest")"
    if _download_model_file "$U2NET_MODEL_URL" "$dest"; then
        print_success "Background-removal model ready"
    else
        print_warning "u2net download failed — 'removebackground' fetches it on first use instead (at $dest)"
    fi
}

download_model() {
    print_step "Download a model?"
    echo ""
    echo "  Lightweight models this project is tuned for (GGUF Q4_K_M, ~5.6GB each):"
    echo "  1. Qwen3.5-9B-Claude-Code  - agentic coding / opencode (reliable tool calls)"
    echo "  2. Qwen3.5-9B-abliterated  - general chat (uncensored); the default model"
    echo "  3. Both (recommended for an 8GB GPU / general use)"
    echo ""
    echo "  Best agentic coder (needs a 12GB+ GPU; partial CPU offload below ~18GB VRAM):"
    echo "  4. Qwen3-Coder-30B-A3B-Instruct (IQ4_XS, ~16GB) - MoE, ~3B active. Far stronger"
    echo "     at multi-step tool use; reliably 1-shots small apps. Point opencode at this."
    echo ""
    echo "  Image model (SDXL): cyberrealisticXL_v100.safetensors - download from CivitAI"
    echo "  (search 'CyberRealistic XL') and drop it in the models dir. Not auto-downloaded"
    echo "  because CivitAI requires an account token."
    echo ""
    read -p "Download a starter LLM? [1/2/3/4/n]: " DOWNLOAD_MODEL

    local MODELS_PATH="/var/lib/posterchanai/models"
    mkdir -p "$MODELS_PATH" 2>/dev/null || true
    # Verified HF repos with these exact filenames (the ones running in production).
    local CC_URL="https://huggingface.co/empero-ai/Qwen3.5-9B-Claude-Code-GGUF/resolve/main/Qwen3.5-9B-Claude-Code-Q4_K_M.gguf"
    local AB_URL="https://huggingface.co/lukey03/Qwen3.5-9B-abliterated-GGUF/resolve/main/Qwen3.5-9B-abliterated-Q4_K_M.gguf"
    local CODER_URL="https://huggingface.co/unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF/resolve/main/Qwen3-Coder-30B-A3B-Instruct-IQ4_XS.gguf"

    case "$DOWNLOAD_MODEL" in
        1)  _download_model_file "$CC_URL" "$MODELS_PATH/Qwen3.5-9B-Claude-Code-Q4_K_M.gguf" ;;
        2)  _download_model_file "$AB_URL" "$MODELS_PATH/Qwen3.5-9B-abliterated-Q4_K_M.gguf" ;;
        3|[Yy]|[Yy][Ee][Ss])
            _download_model_file "$AB_URL" "$MODELS_PATH/Qwen3.5-9B-abliterated-Q4_K_M.gguf"
            _download_model_file "$CC_URL" "$MODELS_PATH/Qwen3.5-9B-Claude-Code-Q4_K_M.gguf"
            ;;
        4)  _download_model_file "$CODER_URL" "$MODELS_PATH/Qwen3-Coder-30B-A3B-Instruct-IQ4_XS.gguf" ;;
        *)  return ;;
    esac
    echo ""
    echo "  Configure in Admin Settings > LLM Model Path (default: the abliterated model)."
    echo "  For agentic coding, point opencode (its own config) at Qwen3-Coder-30B-A3B if you"
    echo "  downloaded it (best), otherwise Qwen3.5-9B-Claude-Code. With a 12-16GB GPU the 30B"
    echo "  auto-fits context via partial CPU offload - leave ollama_num_ctx on 'auto'."
}
