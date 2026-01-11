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

download_model() {
    print_step "Download a model?"
    echo ""
    echo "  Recommended models for local inference:"
    echo "  1. Qwen3-1.7B-abliterated (1.4GB) - Lightweight, fast"
    echo "  2. Qwen3-8B-abliterated (5.9GB) - Fast, good quality (recommended)"
    echo "  3. Qwen2.5-7B-Instruct (7GB) - Fast, good quality"
    echo ""

    read -p "Download a starter model? [1/2/3/n]: " DOWNLOAD_MODEL

    local MODELS_PATH="/var/lib/posterchanai/models"
    local MODEL_URL=""
    local MODEL_FILE=""

    case "$DOWNLOAD_MODEL" in
        1)
            echo "  Downloading Qwen3-1.7B-abliterated Q6_K..."
            MODEL_URL="https://huggingface.co/mradermacher/Qwen3-1.7B-abliterated-GGUF/resolve/main/Qwen3-1.7B-abliterated.Q6_K.gguf"
            MODEL_FILE="$MODELS_PATH/Qwen3-1.7B-abliterated.Q6_K.gguf"
            ;;
        2|[Yy]|[Yy][Ee][Ss])
            echo "  Downloading Qwen3-8B-abliterated Q5_K_M..."
            MODEL_URL="https://huggingface.co/DevQuasar/huihui-ai.Qwen3-8B-abliterated-GGUF/resolve/main/huihui-ai.Qwen3-8B-abliterated.Q5_K_M.gguf"
            MODEL_FILE="$MODELS_PATH/Qwen3-8B-abliterated-Q5_K_M.gguf"
            ;;
        3)
            echo "  Downloading Qwen2.5-7B-Instruct Q5_K_M..."
            MODEL_URL="https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q5_k_m.gguf"
            MODEL_FILE="$MODELS_PATH/Qwen2.5-7B-Instruct-Q5_K_M.gguf"
            ;;
        *)
            return
            ;;
    esac

    if [ -n "$MODEL_URL" ]; then
        local DOWNLOAD_OK=0
        if command -v wget &>/dev/null; then
            wget -q --show-progress -O "$MODEL_FILE" "$MODEL_URL" && DOWNLOAD_OK=1
        elif command -v curl &>/dev/null; then
            curl -L --progress-bar -o "$MODEL_FILE" "$MODEL_URL" && DOWNLOAD_OK=1
        else
            print_warning "Neither wget nor curl found. Please download manually."
            echo "  URL: $MODEL_URL"
            echo "  Save to: $MODEL_FILE"
        fi

        if [ "$DOWNLOAD_OK" = "1" ] && [ -f "$MODEL_FILE" ] && [ -s "$MODEL_FILE" ]; then
            print_success "Model downloaded to $MODEL_FILE"
            echo "  Configure this model in Admin Settings > LLM Model Path"
        elif [ -f "$MODEL_FILE" ]; then
            rm -f "$MODEL_FILE"
            print_error "Download failed. Please try again manually."
        fi
    fi
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
