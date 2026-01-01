#!/bin/bash
# Posterchanai Installer
# Interactive setup for GPU acceleration and systemd service

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

print_banner() {
    echo -e "${CYAN}"
    echo "╔═══════════════════════════════════════════════════════════════╗"
    echo "║                                                               ║"
    echo "║   ${BOLD}🤖 POSTERCHANAI INSTALLER${NC}${CYAN}                                  ║"
    echo "║                                                               ║"
    echo "║   AI Chat with Local LLM Support                              ║"
    echo "║                                                               ║"
    echo "╚═══════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

print_step() {
    echo -e "\n${BLUE}▶ ${BOLD}$1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

detect_gpu() {
    print_step "Detecting GPU..."

    GPU_TYPE="cpu"
    GPU_NAME="CPU Only"

    # Check for Intel GPU
    if lspci 2>/dev/null | grep -i "VGA\|3D" | grep -qi "intel.*arc\|intel.*graphics"; then
        if command -v sycl-ls &>/dev/null || [ -f /opt/intel/oneapi/setvars.sh ] || [ -f ~/intel/oneapi/setvars.sh ]; then
            GPU_TYPE="intel"
            GPU_NAME=$(lspci | grep -i "VGA\|3D" | grep -i intel | head -1 | sed 's/.*: //')
        fi
    fi

    # Check for NVIDIA GPU
    if command -v nvidia-smi &>/dev/null; then
        GPU_TYPE="nvidia"
        GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || echo "NVIDIA GPU")
    fi

    # Check for AMD GPU
    if lspci 2>/dev/null | grep -i "VGA\|3D" | grep -qi "amd\|radeon"; then
        if command -v rocm-smi &>/dev/null; then
            GPU_TYPE="amd"
            GPU_NAME=$(lspci | grep -i "VGA\|3D" | grep -i "amd\|radeon" | head -1 | sed 's/.*: //')
        fi
    fi

    echo -e "  Detected: ${BOLD}$GPU_NAME${NC}"
}

select_backend() {
    print_step "Select inference backend:"
    echo ""
    echo "  1) ${BOLD}Intel Arc GPU${NC} (IPEX-LLM + llama.cpp SYCL)"
    echo "     Best for Intel Arc A770, A750, A380, etc."
    echo ""
    echo "  2) ${BOLD}NVIDIA GPU${NC} (llama.cpp CUDA)"
    echo "     Best for GeForce RTX, Tesla, etc."
    echo ""
    echo "  3) ${BOLD}CPU Only${NC} (llama.cpp)"
    echo "     Works on any system, slower inference"
    echo ""
    echo "  4) ${BOLD}Ollama${NC} (External service)"
    echo "     Use existing Ollama installation"
    echo ""

    # Default based on detection
    case "$GPU_TYPE" in
        intel) DEFAULT=1 ;;
        nvidia) DEFAULT=2 ;;
        *) DEFAULT=3 ;;
    esac

    read -p "Select backend [1-4, default=$DEFAULT]: " BACKEND_CHOICE
    BACKEND_CHOICE=${BACKEND_CHOICE:-$DEFAULT}

    case "$BACKEND_CHOICE" in
        1) BACKEND="intel" ;;
        2) BACKEND="nvidia" ;;
        3) BACKEND="cpu" ;;
        4) BACKEND="ollama" ;;
        *) BACKEND="cpu" ;;
    esac
}

setup_directories() {
    print_step "Setting up directories..."

    # Create upload directory
    UPLOAD_PATH="/var/lib/posterchanai"
    if [ ! -d "$UPLOAD_PATH" ]; then
        sudo mkdir -p "$UPLOAD_PATH"
        sudo chown $(whoami):$(whoami) "$UPLOAD_PATH"
        print_success "Created $UPLOAD_PATH"
    else
        print_success "Upload directory exists"
    fi

    # Create models directory
    MODELS_PATH="$UPLOAD_PATH/models"
    if [ ! -d "$MODELS_PATH" ]; then
        sudo mkdir -p "$MODELS_PATH"
        sudo chown $(whoami):$(whoami) "$MODELS_PATH"
        print_success "Created $MODELS_PATH"
    fi
}

setup_python_env() {
    print_step "Setting up Python environment..."

    VENV_NAME="venv"
    if [ "$BACKEND" = "intel" ]; then
        VENV_NAME="venv-ipex"
    fi

    if [ ! -d "$VENV_NAME" ]; then
        python3 -m venv "$VENV_NAME"
        print_success "Created virtual environment: $VENV_NAME"
    else
        print_success "Virtual environment exists: $VENV_NAME"
    fi

    source "$VENV_NAME/bin/activate"
    pip install --upgrade pip -q

    print_step "Installing Python dependencies..."
    pip install -r requirements.txt -q
    print_success "Base dependencies installed"
}

setup_llama_cpp() {
    print_step "Installing llama-cpp-python..."

    case "$BACKEND" in
        intel)
            # Source Intel oneAPI
            ONEAPI_PATH=""
            if [ -f /opt/intel/oneapi/2025.0/oneapi-vars.sh ]; then
                ONEAPI_PATH="/opt/intel/oneapi/2025.0/oneapi-vars.sh"
            elif [ -f /opt/intel/oneapi/2024.2/oneapi-vars.sh ]; then
                ONEAPI_PATH="/opt/intel/oneapi/2024.2/oneapi-vars.sh"
            elif [ -f /opt/intel/oneapi/setvars.sh ]; then
                ONEAPI_PATH="/opt/intel/oneapi/setvars.sh"
            elif [ -f ~/intel/oneapi/setvars.sh ]; then
                ONEAPI_PATH="$HOME/intel/oneapi/setvars.sh"
            else
                print_error "Intel oneAPI not found!"
                echo ""
                echo "  Please install Intel oneAPI Base Toolkit:"
                echo "  https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit-download.html"
                echo ""
                echo "  Or install via package manager:"
                echo "    # Add Intel repo"
                echo "    wget -O- https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB | sudo gpg --dearmor -o /usr/share/keyrings/oneapi-archive-keyring.gpg"
                echo "    echo 'deb [signed-by=/usr/share/keyrings/oneapi-archive-keyring.gpg] https://apt.repos.intel.com/oneapi all main' | sudo tee /etc/apt/sources.list.d/oneAPI.list"
                echo "    sudo apt update && sudo apt install intel-oneapi-base-toolkit"
                exit 1
            fi

            source "$ONEAPI_PATH" --force >/dev/null 2>&1
            print_success "Intel oneAPI loaded from $ONEAPI_PATH"

            # Create libittnotify stub if not exists (fixes VTune symbol errors)
            if [ ! -f /usr/local/lib/libittnotify.so ]; then
                print_step "Creating VTune stub library..."
                STUB_DIR=$(mktemp -d)
                cat > "$STUB_DIR/ittnotify_stub.c" << 'STUBCODE'
// Stub for Intel VTune symbols
void __itt_pause(void) {}
void __itt_resume(void) {}
int __itt_api_init(void) { return 0; }
void* __itt_null = 0;
int iJIT_NotifyEvent(int, void*) { return 0; }
STUBCODE
                gcc -shared -fPIC -o "$STUB_DIR/libittnotify.so" "$STUB_DIR/ittnotify_stub.c" 2>/dev/null || true
                if [ -f "$STUB_DIR/libittnotify.so" ]; then
                    sudo cp "$STUB_DIR/libittnotify.so" /usr/local/lib/
                    sudo ldconfig
                    print_success "VTune stub library installed"
                fi
                rm -rf "$STUB_DIR"
            fi

            # Install pinned llama-cpp-python version (tested working)
            LLAMA_CPP_VERSION="0.3.16"
            echo "  Building llama-cpp-python==$LLAMA_CPP_VERSION with Intel SYCL..."
            echo "  This may take 5-10 minutes..."
            export CMAKE_ARGS="-DGGML_SYCL=ON -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx"
            pip install "llama-cpp-python==$LLAMA_CPP_VERSION" --force-reinstall --no-cache-dir -q
            ;;

        nvidia)
            echo "  Building with CUDA backend..."
            export CMAKE_ARGS="-DGGML_CUDA=ON"
            pip install llama-cpp-python --force-reinstall --no-cache-dir -q
            ;;

        cpu)
            echo "  Building CPU-only version..."
            pip install llama-cpp-python --force-reinstall --no-cache-dir -q
            ;;

        ollama)
            print_success "Using Ollama backend (no llama-cpp-python needed)"
            return
            ;;
    esac

    print_success "llama-cpp-python installed"
}

setup_systemd() {
    print_step "Configure systemd service?"
    read -p "Install as systemd service? [Y/n]: " INSTALL_SERVICE
    INSTALL_SERVICE=${INSTALL_SERVICE:-Y}

    if [[ ! "$INSTALL_SERVICE" =~ ^[Yy] ]]; then
        print_warning "Skipping systemd setup"
        return
    fi

    SERVICE_NAME="posterchanai"
    if [ "$BACKEND" = "intel" ]; then
        SERVICE_NAME="posterchanai-ipex"
    fi

    print_step "Creating systemd service: $SERVICE_NAME"

    # Create run script for the backend
    RUN_SCRIPT="$SCRIPT_DIR/run-$BACKEND.sh"

    case "$BACKEND" in
        intel)
            cat > "$RUN_SCRIPT" << 'SCRIPT'
#!/bin/bash
# IPEX-LLM wrapper script for Intel Arc GPU

# Set Intel oneAPI environment
if [ -f /opt/intel/oneapi/2025.0/oneapi-vars.sh ]; then
    source /opt/intel/oneapi/2025.0/oneapi-vars.sh --force
elif [ -f /opt/intel/oneapi/2024.2/oneapi-vars.sh ]; then
    source /opt/intel/oneapi/2024.2/oneapi-vars.sh --force
elif [ -f /opt/intel/oneapi/setvars.sh ]; then
    source /opt/intel/oneapi/setvars.sh --force
fi

# Preload VTune stub if available
[ -f /usr/local/lib/libittnotify.so ] && export LD_PRELOAD=/usr/local/lib/libittnotify.so

# IPEX-LLM optimizations
export ENABLE_SDP_FUSION=1
export SYCL_CACHE_PERSISTENT=1
export BIGDL_LLM_XMX_DISABLED=1
export ZES_ENABLE_SYSMAN=1
export TORCH_DEVICE_BACKEND_AUTOLOAD=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
exec setarch $(uname -m) -X "$SCRIPT_DIR/venv-ipex/bin/python" run.py "$@"
SCRIPT
            ;;

        nvidia)
            cat > "$RUN_SCRIPT" << 'SCRIPT'
#!/bin/bash
# NVIDIA CUDA wrapper script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
exec "$SCRIPT_DIR/venv/bin/python" run.py "$@"
SCRIPT
            ;;

        *)
            cat > "$RUN_SCRIPT" << 'SCRIPT'
#!/bin/bash
# CPU/Ollama wrapper script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
exec "$SCRIPT_DIR/venv/bin/python" run.py "$@"
SCRIPT
            ;;
    esac

    chmod +x "$RUN_SCRIPT"

    # Create systemd service file
    VENV_PATH="$SCRIPT_DIR/venv"
    [ "$BACKEND" = "intel" ] && VENV_PATH="$SCRIPT_DIR/venv-ipex"

    sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null << EOF
[Unit]
Description=Posterchan AI ($BACKEND backend)
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$SCRIPT_DIR
Environment="PATH=$VENV_PATH/bin:/usr/local/bin:/usr/bin"
Environment="VIRTUAL_ENV=$VENV_PATH"
ExecStart=$RUN_SCRIPT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    print_success "Created systemd service: $SERVICE_NAME"

    read -p "Enable and start service now? [Y/n]: " START_NOW
    START_NOW=${START_NOW:-Y}

    if [[ "$START_NOW" =~ ^[Yy] ]]; then
        sudo systemctl enable $SERVICE_NAME
        sudo systemctl start $SERVICE_NAME
        sleep 3
        if systemctl is-active --quiet $SERVICE_NAME; then
            print_success "Service started successfully"
        else
            print_error "Service failed to start. Check: sudo journalctl -u $SERVICE_NAME -n 50"
        fi
    fi
}

download_model() {
    print_step "Download a model?"
    echo ""
    echo "  Recommended models for local inference:"
    echo "  • Qwen2.5-7B-Instruct (7GB) - Fast, good quality"
    echo "  • Mistral-7B-Instruct (7GB) - Great all-rounder"
    echo "  • Llama-3.1-8B-Instruct (8GB) - Meta's latest"
    echo ""

    read -p "Download a starter model? [y/N]: " DOWNLOAD_MODEL

    if [[ "$DOWNLOAD_MODEL" =~ ^[Yy] ]]; then
        MODELS_PATH="/var/lib/posterchanai/models"
        echo ""
        echo "  Downloading Qwen2.5-7B-Instruct Q5_K_M..."

        MODEL_URL="https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q5_k_m.gguf"
        MODEL_FILE="$MODELS_PATH/qwen2.5-7b-instruct-q5_k_m.gguf"

        if command -v wget &>/dev/null; then
            wget -q --show-progress -O "$MODEL_FILE" "$MODEL_URL"
        elif command -v curl &>/dev/null; then
            curl -L --progress-bar -o "$MODEL_FILE" "$MODEL_URL"
        else
            print_warning "Neither wget nor curl found. Please download manually."
            echo "  URL: $MODEL_URL"
            echo "  Save to: $MODEL_FILE"
        fi

        if [ -f "$MODEL_FILE" ]; then
            print_success "Model downloaded to $MODEL_FILE"
            echo ""
            echo "  Configure this model in Admin Settings > LLM Model Path"
        fi
    fi
}

print_summary() {
    echo ""
    echo -e "${GREEN}╔═══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  ${BOLD}Installation Complete!${NC}${GREEN}                                      ║${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "  Backend: $BACKEND"
    echo "  Service: $SERVICE_NAME"
    echo ""
    echo "  ${BOLD}Access:${NC} http://localhost:3051"
    echo "  ${BOLD}Login:${NC}  admin / admin"
    echo ""
    echo "  ${BOLD}Commands:${NC}"
    echo "    Start:   sudo systemctl start $SERVICE_NAME"
    echo "    Stop:    sudo systemctl stop $SERVICE_NAME"
    echo "    Logs:    sudo journalctl -u $SERVICE_NAME -f"
    echo ""
}

# Main
print_banner
detect_gpu
select_backend
setup_directories
setup_python_env

if [ "$BACKEND" != "ollama" ]; then
    setup_llama_cpp
fi

setup_systemd
download_model
print_summary
