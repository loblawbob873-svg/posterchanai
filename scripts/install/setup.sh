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

    # Create data directory for ChromaDB
    local DATA_PATH="$SCRIPT_DIR/data"
    if [ ! -d "$DATA_PATH" ]; then
        mkdir -p "$DATA_PATH/chromadb"
        print_success "Created $DATA_PATH/chromadb (RAG vector store)"
    fi
}

setup_python_env() {
    print_step "Setting up Python environment..."

    # Intel Arc uses venv-ipex for chat (IPEX-LLM), others use venv
    local VENV_NAME="venv"
    if [ "$BACKEND" = "intel" ]; then
        VENV_NAME="venv-ipex"
    fi

    # Export for use by other modules
    export CHAT_VENV_NAME="$VENV_NAME"

    if [ ! -d "$VENV_NAME" ]; then
        python3 -m venv "$VENV_NAME"
        print_success "Created virtual environment: $VENV_NAME"
    else
        print_success "Virtual environment exists: $VENV_NAME"
    fi

    source "$VENV_NAME/bin/activate"
    pip install --upgrade pip -q

    # Intel IPEX requires numpy<2
    if [ "$BACKEND" = "intel" ]; then
        print_step "Installing numpy<2 (required for IPEX compatibility)..."
        pip install "numpy<2" -q
    fi

    print_step "Installing Python dependencies..."
    pip install -r requirements.txt -q

    # Ensure numpy<2 for Intel (requirements.txt may have overwritten it)
    if [ "$BACKEND" = "intel" ]; then
        pip install "numpy<2" -q
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

    # Determine llm_backend value
    local DB_LLM_BACKEND="ollama"
    case "$BACKEND" in
        intel) DB_LLM_BACKEND="ipex" ;;
        nvidia|amd|cpu) DB_LLM_BACKEND="native" ;;
        ollama) DB_LLM_BACKEND="ollama" ;;
    esac

    # Initialize database if needed
    if [ ! -f "posterchanai.db" ]; then
        print_step "Initializing database..."
        local VENV_PATH="venv"
        [ "$BACKEND" = "intel" ] && VENV_PATH="venv-ipex"
        "$VENV_PATH/bin/python" -c "from app.database import init_db; init_db()" 2>/dev/null || true
    fi

    # Update llm_backend setting
    if [ -f "posterchanai.db" ]; then
        sqlite3 posterchanai.db "INSERT OR REPLACE INTO settings (key, value) VALUES ('llm_backend', '$DB_LLM_BACKEND');" 2>/dev/null
        print_success "LLM backend set to: $DB_LLM_BACKEND"
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

download_model() {
    print_step "Download a model?"
    echo ""
    echo "  Models this project is tuned for (GGUF Q4_K_M, ~5.6GB each):"
    echo "  1. Qwen3.5-9B-Claude-Code  - agentic coding / opencode (reliable tool calls)"
    echo "  2. Qwen3.5-9B-abliterated  - general chat (uncensored); the default model"
    echo "  3. Both (recommended)"
    echo ""
    echo "  Image model (SDXL): cyberrealisticXL_v100.safetensors - download from CivitAI"
    echo "  (search 'CyberRealistic XL') and drop it in the models dir. Not auto-downloaded"
    echo "  because CivitAI requires an account token."
    echo ""
    read -p "Download a starter LLM? [1/2/3/n]: " DOWNLOAD_MODEL

    local MODELS_PATH="/var/lib/posterchanai/models"
    mkdir -p "$MODELS_PATH" 2>/dev/null || true
    # Verified HF repos with these exact filenames (the ones running in production).
    local CC_URL="https://huggingface.co/empero-ai/Qwen3.5-9B-Claude-Code-GGUF/resolve/main/Qwen3.5-9B-Claude-Code-Q4_K_M.gguf"
    local AB_URL="https://huggingface.co/lukey03/Qwen3.5-9B-abliterated-GGUF/resolve/main/Qwen3.5-9B-abliterated-Q4_K_M.gguf"

    case "$DOWNLOAD_MODEL" in
        1)  _download_model_file "$CC_URL" "$MODELS_PATH/Qwen3.5-9B-Claude-Code-Q4_K_M.gguf" ;;
        2)  _download_model_file "$AB_URL" "$MODELS_PATH/Qwen3.5-9B-abliterated-Q4_K_M.gguf" ;;
        3|[Yy]|[Yy][Ee][Ss])
            _download_model_file "$AB_URL" "$MODELS_PATH/Qwen3.5-9B-abliterated-Q4_K_M.gguf"
            _download_model_file "$CC_URL" "$MODELS_PATH/Qwen3.5-9B-Claude-Code-Q4_K_M.gguf"
            ;;
        *)  return ;;
    esac
    echo ""
    echo "  Configure in Admin Settings > LLM Model Path (default: the abliterated model)."
    echo "  Point opencode (its own config) at Qwen3.5-9B-Claude-Code for agentic coding."
}

setup_mcp_server() {
    print_step "MCP Server (Integrated)"
    echo ""
    echo "  The MCP server is integrated into the main app!"
    echo "  It starts automatically - no separate service needed."
    echo ""
    echo "  Features:"
    echo "    • Exposes RAG search to Continue.dev, Claude Desktop, etc."
    echo "    • Configure in Admin > Services > MCP Server"
    echo "    • Default port: 8808"
    echo ""
    print_success "MCP server will start automatically with the main app"
}

