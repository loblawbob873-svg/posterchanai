#!/bin/bash
# Posterchanai Installer
# Interactive setup for GPU acceleration and systemd service
# Supports modular installation: LLM, Image Generation, or Full Stack

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

# Installation mode flags
INSTALL_LLM=0
INSTALL_IMAGE=0
LLM_BACKEND=""
IMAGE_BACKEND=""

print_banner() {
    echo -e "${CYAN}"
    echo -e "╔═══════════════════════════════════════════════════════════════╗"
    echo -e "║                                                               ║"
    echo -e "║   ${BOLD}POSTERCHANAI INSTALLER${NC}${CYAN}                                      ║"
    echo -e "║                                                               ║"
    echo -e "║   AI Chat + Image Generation                                  ║"
    echo -e "║                                                               ║"
    echo -e "╚═══════════════════════════════════════════════════════════════╝"
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

detect_distro() {
    if [ -f /etc/gentoo-release ]; then
        DISTRO="gentoo"
    elif [ -f /etc/arch-release ]; then
        DISTRO="arch"
    elif [ -f /etc/debian_version ]; then
        DISTRO="debian"
    elif [ -f /etc/fedora-release ]; then
        DISTRO="fedora"
    else
        DISTRO="unknown"
    fi
}

check_dependencies() {
    print_step "Checking system dependencies..."

    MISSING_DEPS=""

    # Check for Python 3.10+
    if ! command -v python3 &>/dev/null; then
        MISSING_DEPS="$MISSING_DEPS python3"
    else
        PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        PY_OK=$(python3 -c "import sys; print(1 if sys.version_info >= (3, 10) else 0)")
        if [ "$PY_OK" = "1" ]; then
            print_success "Python $PY_VERSION"
        else
            print_warning "Python $PY_VERSION detected. Python 3.10+ recommended."
        fi
    fi

    # Check for pip
    if ! command -v pip3 &>/dev/null && ! python3 -m pip --version &>/dev/null 2>&1; then
        MISSING_DEPS="$MISSING_DEPS pip"
    fi

    # Check for gcc (needed to compile llama-cpp-python)
    if ! command -v gcc &>/dev/null; then
        MISSING_DEPS="$MISSING_DEPS gcc"
    fi

    # Check for cmake
    if ! command -v cmake &>/dev/null; then
        MISSING_DEPS="$MISSING_DEPS cmake"
    fi

    # Check for git
    if ! command -v git &>/dev/null; then
        MISSING_DEPS="$MISSING_DEPS git"
    fi

    if [ -n "$MISSING_DEPS" ]; then
        print_error "Missing dependencies:$MISSING_DEPS"
        echo ""
        show_install_instructions
        exit 1
    fi

    print_success "All base dependencies found"
}

show_install_instructions() {
    detect_distro

    echo -e "${YELLOW}Please install the required packages:${NC}"
    echo ""

    case "$DISTRO" in
        gentoo)
            echo -e "${BOLD}Gentoo Linux:${NC}"
            echo ""
            echo "  # Base dependencies"
            echo "  emerge -av dev-lang/python dev-python/pip dev-build/cmake sys-devel/gcc"
            echo ""
            echo "  # For AMD GPU (ROCm):"
            echo "  # First, add ROCm packages to package.accept_keywords (they're ~amd64):"
            echo "  echo -e 'dev-build/rocm-cmake\ndev-util/hipcc\ndev-libs/rocm-core\ndev-libs/roct-thunk-interface\ndev-libs/rocm-device-libs\ndev-libs/rocr-runtime\ndev-libs/rocm-comgr\ndev-util/rocminfo\ndev-util/rocm-smi\ndev-libs/rocm-opencl-runtime\ndev-util/hip\nsci-libs/hipBLAS\nsci-libs/hipBLAS-common\nsci-libs/rocBLAS\nsci-libs/rocSOLVER\ndev-util/Tensile' | sudo tee /etc/portage/package.accept_keywords/rocm"
            echo ""
            echo "  # Then install ROCm + hipBLAS (required for llama.cpp):"
            echo "  # NOTE: Requires 30-50GB free in /var/tmp for building rocBLAS/Tensile!"
            echo "  # If using tmpfs/zram, unmount it first: sudo umount /var/tmp"
            echo "  emerge -av dev-libs/rocm-opencl-runtime dev-util/hip dev-libs/rocr-runtime sci-libs/hipBLAS"
            echo "  # Supported: RX 6000/7000 series, some RX 5000"
            echo ""
            echo "  # For Intel Arc GPU (optional):"
            echo "  # First, disable distcc for intel-graphics-compiler (build fails with distcc):"
            echo "  echo 'dev-util/intel-graphics-compiler no-distcc.conf' | sudo tee -a /etc/portage/package.env/intel-graphics-compiler"
            echo "  emerge -av dev-libs/intel-compute-runtime dev-libs/level-zero"
            echo "  # Install Intel oneAPI from Intel's repo or manually"
            echo "  # See: https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit-download.html"
            echo ""
            echo "  # For NVIDIA GPU (optional):"
            echo "  emerge -av x11-drivers/nvidia-drivers dev-util/nvidia-cuda-toolkit"
            echo ""
            echo "  # For OCR support (optional):"
            echo "  emerge -av app-text/tesseract"
            echo ""
            echo "  # For PDF support (optional):"
            echo "  emerge -av app-text/poppler"
            echo ""
            echo "  # For HEIC image support (optional):"
            echo "  emerge -av media-libs/libheif"
            ;;
        arch)
            echo -e "${BOLD}Arch Linux:${NC}"
            echo "  pacman -S python python-pip cmake gcc git"
            echo ""
            echo "  # For AMD GPU (ROCm):"
            echo "  pacman -S rocm-hip-sdk rocm-opencl-sdk"
            echo "  # Or from AUR: yay -S rocm-hip-runtime"
            echo ""
            echo "  # For Intel Arc GPU: Install intel-oneapi-basekit from AUR"
            echo "  # For NVIDIA GPU: pacman -S nvidia cuda"
            ;;
        debian)
            echo -e "${BOLD}Debian/Ubuntu:${NC}"
            echo "  apt install python3 python3-pip python3-venv cmake build-essential git"
            echo ""
            echo "  # For AMD GPU (ROCm):"
            echo "  # Add AMD's repo and install:"
            echo "  wget https://repo.radeon.com/amdgpu-install/latest/ubuntu/\$(lsb_release -cs)/amdgpu-install_6.0.60002-1_all.deb"
            echo "  apt install ./amdgpu-install_*.deb"
            echo "  amdgpu-install --usecase=rocm"
            echo "  # Or see: https://rocm.docs.amd.com/projects/install-on-linux/en/latest/"
            echo ""
            echo "  # For Intel Arc GPU:"
            echo "  # See: https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit-download.html"
            echo "  # For NVIDIA GPU: apt install nvidia-driver nvidia-cuda-toolkit"
            ;;
        fedora)
            echo -e "${BOLD}Fedora:${NC}"
            echo "  dnf install python3 python3-pip cmake gcc-c++ git"
            echo ""
            echo "  # For AMD GPU (ROCm):"
            echo "  # Add AMD's repo and install:"
            echo "  dnf install https://repo.radeon.com/amdgpu-install/latest/rhel/\$(rpm -E %rhel)/amdgpu-install-*.noarch.rpm"
            echo "  amdgpu-install --usecase=rocm"
            echo ""
            echo "  # For NVIDIA GPU: dnf install nvidia-driver cuda"
            ;;
        *)
            echo "  Please install: python3, pip, cmake, gcc, git"
            echo ""
            echo "  # For AMD GPU (ROCm):"
            echo "  # See: https://rocm.docs.amd.com/projects/install-on-linux/en/latest/"
            ;;
    esac
    echo ""
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

select_components() {
    print_step "Select installation type:"
    echo ""
    echo -e "  1) ${BOLD}Full Stack${NC} (Recommended)"
    echo "     LLM + Image Generation - Everything you need"
    echo ""
    echo -e "  2) ${BOLD}LLM Only${NC}"
    echo "     Chat and text generation only (no image features)"
    echo ""
    echo -e "  3) ${BOLD}Image Only${NC}"
    echo "     Image generation only (use external LLM like Ollama)"
    echo ""
    echo -e "  4) ${BOLD}Lightweight${NC}"
    echo "     Web UI only (use external Ollama + ComfyUI)"
    echo ""

    read -p "Select installation type [1-4, default=1]: " INSTALL_TYPE
    INSTALL_TYPE=${INSTALL_TYPE:-1}

    case "$INSTALL_TYPE" in
        1)
            INSTALL_LLM=1
            INSTALL_IMAGE=1
            echo -e "  ${GREEN}✓ Full Stack: LLM + Image Generation${NC}"
            ;;
        2)
            INSTALL_LLM=1
            INSTALL_IMAGE=0
            echo -e "  ${GREEN}✓ LLM Only${NC}"
            ;;
        3)
            INSTALL_LLM=0
            INSTALL_IMAGE=1
            echo -e "  ${GREEN}✓ Image Only${NC}"
            ;;
        4)
            INSTALL_LLM=0
            INSTALL_IMAGE=0
            echo -e "  ${GREEN}✓ Lightweight (external services)${NC}"
            ;;
        *)
            INSTALL_LLM=1
            INSTALL_IMAGE=1
            ;;
    esac
}

select_backend() {
    # Skip if not installing LLM
    if [ "$INSTALL_LLM" = "0" ]; then
        LLM_BACKEND="ollama"  # Default to external Ollama
        return
    fi

    print_step "Select LLM inference backend:"
    echo ""
    echo -e "  1) ${BOLD}Intel Arc GPU${NC} (IPEX-LLM + llama.cpp SYCL)"
    echo "     Best for Intel Arc A770, A750, A380, etc."
    echo ""
    echo -e "  2) ${BOLD}NVIDIA GPU${NC} (llama.cpp CUDA)"
    echo "     Best for GeForce RTX, Tesla, etc."
    echo ""
    echo -e "  3) ${BOLD}AMD GPU${NC} (llama.cpp ROCm/HIP)"
    echo "     Best for Radeon RX 6000/7000 series"
    echo -e "     ${YELLOW}Note: Requires ROCm installed, ~5GB download for PyTorch${NC}"
    echo ""
    echo -e "  4) ${BOLD}CPU Only${NC} (llama.cpp)"
    echo "     Works on any system, slower inference"
    echo ""
    echo -e "  5) ${BOLD}Ollama${NC} (External service)"
    echo "     Use existing Ollama installation"
    echo ""

    # Default based on detection
    case "$GPU_TYPE" in
        intel) DEFAULT=1 ;;
        nvidia) DEFAULT=2 ;;
        amd) DEFAULT=3 ;;
        *) DEFAULT=4 ;;
    esac

    read -p "Select LLM backend [1-5, default=$DEFAULT]: " BACKEND_CHOICE
    BACKEND_CHOICE=${BACKEND_CHOICE:-$DEFAULT}

    case "$BACKEND_CHOICE" in
        1) LLM_BACKEND="intel" ;;
        2) LLM_BACKEND="nvidia" ;;
        3) LLM_BACKEND="amd" ;;
        4) LLM_BACKEND="cpu" ;;
        5) LLM_BACKEND="ollama" ;;
        *) LLM_BACKEND="cpu" ;;
    esac

    # Set BACKEND for backward compatibility with rest of script
    BACKEND="$LLM_BACKEND"
}

select_image_backend() {
    # Skip if not installing image generation
    if [ "$INSTALL_IMAGE" = "0" ]; then
        IMAGE_BACKEND="comfyui"  # Default to external ComfyUI
        return
    fi

    print_step "Select image generation backend:"
    echo ""
    echo -e "  1) ${BOLD}Native (diffusers)${NC} - Recommended"
    echo "     Built-in Stable Diffusion using HuggingFace diffusers"
    echo "     Supports: NVIDIA (CUDA), Intel Arc (XPU), AMD (ROCm), CPU"
    echo ""
    echo -e "  2) ${BOLD}ComfyUI${NC} (External)"
    echo "     Use existing ComfyUI installation"
    echo "     More features but requires separate setup"
    echo ""

    # Default based on GPU detection
    DEFAULT=1

    read -p "Select image backend [1-2, default=$DEFAULT]: " IMAGE_CHOICE
    IMAGE_CHOICE=${IMAGE_CHOICE:-$DEFAULT}

    case "$IMAGE_CHOICE" in
        1) IMAGE_BACKEND="native" ;;
        2) IMAGE_BACKEND="comfyui" ;;
        *) IMAGE_BACKEND="native" ;;
    esac

    # Show AMD-specific warnings for native image generation
    if [ "$IMAGE_BACKEND" = "native" ] && [ "$GPU_TYPE" = "amd" ]; then
        echo ""
        print_warning "AMD ROCm Image Generation Notes:"
        echo ""
        echo "  • PyTorch ROCm nightly is ~5GB download"
        echo "  • Requires 10GB+ disk space for installation"
        echo "  • SDXL models need 8GB+ VRAM (12GB recommended)"
        echo "  • Close other GPU apps before generating images"
        echo "  • First generation is slow (shader compilation)"
        echo ""
        echo "  If you have <8GB VRAM, consider using ComfyUI instead"
        echo "  or SD 1.5 models which use less memory."
        echo ""
        read -p "Continue with native image generation? [Y/n]: " CONTINUE_AMD
        if [[ "$CONTINUE_AMD" =~ ^[Nn] ]]; then
            IMAGE_BACKEND="comfyui"
            echo "  Switched to ComfyUI backend"
        fi
    fi
}

setup_directories() {
    print_step "Setting up directories..."

    # Create upload directory
    UPLOAD_PATH="/var/lib/posterchanai"
    if [ ! -d "$UPLOAD_PATH" ]; then
        sudo mkdir -p "$UPLOAD_PATH"
        sudo chown "$(whoami)":"$(whoami)" "$UPLOAD_PATH"
        print_success "Created $UPLOAD_PATH"
    else
        print_success "Upload directory exists"
    fi

    # Create models directory
    MODELS_PATH="$UPLOAD_PATH/models"
    if [ ! -d "$MODELS_PATH" ]; then
        sudo mkdir -p "$MODELS_PATH"
        sudo chown "$(whoami)":"$(whoami)" "$MODELS_PATH"
        print_success "Created $MODELS_PATH"
    fi

    # Create data directory for ChromaDB (RAG vector store)
    DATA_PATH="$SCRIPT_DIR/data"
    if [ ! -d "$DATA_PATH" ]; then
        mkdir -p "$DATA_PATH/chromadb"
        print_success "Created $DATA_PATH/chromadb (RAG vector store)"
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

    # Intel IPEX requires numpy<2 (compiled with numpy 1.x)
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
}

setup_llama_cpp() {
    print_step "Installing llama-cpp-python..."

    case "$BACKEND" in
        intel)
            # Gentoo-specific: Configure package.env for intel-graphics-compiler
            # (distcc causes build failures for this package)
            if [ "$DISTRO" = "gentoo" ]; then
                IGC_ENV_FILE="/etc/portage/package.env/intel-graphics-compiler"
                if [ ! -f "$IGC_ENV_FILE" ] || ! grep -q "no-distcc.conf" "$IGC_ENV_FILE" 2>/dev/null; then
                    print_step "Configuring Gentoo package.env for Intel graphics compiler..."
                    sudo mkdir -p /etc/portage/package.env
                    echo 'dev-util/intel-graphics-compiler no-distcc.conf' | sudo tee -a "$IGC_ENV_FILE" > /dev/null
                    print_success "Added no-distcc.conf for intel-graphics-compiler"
                fi
            fi

            # Check for Level Zero (required for Intel GPU compute)
            if ! ldconfig -p | grep -q libze_loader; then
                print_warning "Level Zero (libze_loader) not found!"
                echo ""
                echo "  This is required for Intel GPU acceleration with IPEX-LLM."
                case "$DISTRO" in
                    gentoo) echo "  Install with: sudo emerge -av dev-libs/level-zero" ;;
                    arch) echo "  Install with: sudo pacman -S level-zero-loader" ;;
                    debian) echo "  Install with: sudo apt install level-zero" ;;
                    *) echo "  Please install the level-zero package for your distribution." ;;
                esac
                echo ""
                read -p "  Continue anyway? [y/N]: " CONTINUE_LZ
                if [[ ! "$CONTINUE_LZ" =~ ^[Yy] ]]; then
                    exit 1
                fi
            fi

            # Check for patchelf (needed to fix executable stack on modern glibc)
            if ! command -v patchelf &>/dev/null; then
                print_warning "patchelf not found!"
                echo ""
                echo "  This is needed to fix IPEX libraries on systems with glibc 2.41+."
                case "$DISTRO" in
                    gentoo) echo "  Install with: sudo emerge -av dev-util/patchelf" ;;
                    arch) echo "  Install with: sudo pacman -S patchelf" ;;
                    debian) echo "  Install with: sudo apt install patchelf" ;;
                    *) echo "  Please install patchelf for your distribution." ;;
                esac
                echo ""
                read -p "  Continue anyway? [y/N]: " CONTINUE_PF
                if [[ ! "$CONTINUE_PF" =~ ^[Yy] ]]; then
                    exit 1
                fi
            fi

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

            # Install IPEX-LLM (Intel's optimized LLM backend)
            echo "  Installing IPEX-LLM..."
            pip install --pre --upgrade ipex-llm[cpp] -q

            # Fix executable stack issue on modern glibc (2.41+)
            if command -v patchelf &>/dev/null; then
                echo "  Fixing IPEX library executable stack flags..."
                IPEX_LIB=$(find venv-ipex/lib -name "libintel-ext-pt-cpu.so" 2>/dev/null | head -1)
                if [ -n "$IPEX_LIB" ]; then
                    patchelf --clear-execstack "$IPEX_LIB" 2>/dev/null && print_success "Fixed executable stack on IPEX library" || true
                fi
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

            # Check GCC version - CUDA has strict GCC version requirements
            GCC_MAJOR=$(gcc -dumpversion | cut -d. -f1)
            CUDA_HOST_COMPILER=""

            if [ "$GCC_MAJOR" -gt 14 ]; then
                print_warning "GCC $GCC_MAJOR detected, but CUDA requires GCC 14 or earlier"

                # Look for compatible GCC versions (prefer newest compatible)
                for ver in 14 13 12 11; do
                    if [ -x "/usr/bin/gcc-$ver" ] && [ -x "/usr/bin/g++-$ver" ]; then
                        CUDA_HOST_COMPILER="/usr/bin/gcc-$ver"
                        print_success "Found compatible compiler: gcc-$ver"
                        break
                    fi
                done

                if [ -z "$CUDA_HOST_COMPILER" ]; then
                    print_error "No compatible GCC found (need GCC 14 or earlier)"
                    echo ""
                    echo "  Please install GCC 14:"
                    case "$DISTRO" in
                        gentoo)
                            echo "    emerge -av sys-devel/gcc:14"
                            ;;
                        arch)
                            echo "    pacman -S gcc13  # or install gcc14 from AUR"
                            ;;
                        debian)
                            echo "    apt install gcc-14 g++-14"
                            ;;
                        fedora)
                            echo "    dnf install gcc g++  # ensure version <= 14"
                            ;;
                        *)
                            echo "    Install GCC 14 or earlier for your distribution"
                            ;;
                    esac
                    exit 1
                fi

                # Set environment for CUDA compilation with older GCC
                export CC="$CUDA_HOST_COMPILER"
                export CXX="${CUDA_HOST_COMPILER/gcc/g++}"
                export CUDAHOSTCXX="$CUDA_HOST_COMPILER"
                export CMAKE_ARGS="-DGGML_CUDA=ON -DCMAKE_CUDA_HOST_COMPILER=$CUDA_HOST_COMPILER"
            else
                export CMAKE_ARGS="-DGGML_CUDA=ON"
            fi

            echo "  This may take 5-10 minutes..."
            pip install llama-cpp-python --force-reinstall --no-cache-dir -q
            ;;

        amd)
            # Check for ROCm
            if ! command -v rocminfo &>/dev/null && [ ! -d /opt/rocm ]; then
                print_error "ROCm not found!"
                echo ""
                echo "  Please install ROCm first. See show_install_instructions for your distro."
                echo "  After installing ROCm, run this installer again."
                exit 1
            fi

            # Detect GPU architecture for HSA_OVERRIDE_GFX_VERSION
            GFX_VERSION=""
            if command -v rocminfo &>/dev/null; then
                GFX_VERSION=$(rocminfo 2>/dev/null | grep -o 'gfx[0-9]*' | head -1)
                if [ -n "$GFX_VERSION" ]; then
                    print_success "Detected AMD GPU: $GFX_VERSION"
                fi
            fi

            # Gentoo-specific warning about llama-cpp-python ROCm build issues
            if [ "$DISTRO" = "gentoo" ]; then
                echo ""
                print_warning "Gentoo ROCm Note:"
                echo "  llama-cpp-python may not build with HIP support on Gentoo"
                echo "  due to non-standard library paths (/usr/lib64 vs /opt/rocm)."
                echo "  The installer will build CPU-only version as fallback."
                echo ""
                echo "  For GPU LLM inference on Gentoo, consider:"
                echo "    1. Use Ollama with ROCm support (pre-built)"
                echo "    2. Build llama.cpp manually with correct cmake paths"
                echo "  See README.md 'Known Issues' section for details."
                echo ""
                echo "  Note: Image generation (diffusers) works fine with ROCm."
                echo ""
            fi

            echo "  Building with ROCm/HIP backend (may fall back to CPU)..."
            export CMAKE_ARGS="-DGGML_HIP=ON"
            # Set HIP path - try /opt/rocm first, fall back to /usr for Gentoo
            if [ -d /opt/rocm ]; then
                export HIP_PATH=/opt/rocm
                export ROCM_PATH=/opt/rocm
            else
                export HIP_PATH=/usr
                export ROCM_PATH=/usr
                export CMAKE_PREFIX_PATH="/usr/lib64/cmake:${CMAKE_PREFIX_PATH:-}"
            fi
            pip install llama-cpp-python --force-reinstall --no-cache-dir -q || {
                print_warning "HIP build failed, falling back to CPU-only..."
                unset CMAKE_ARGS
                pip install llama-cpp-python --force-reinstall --no-cache-dir -q
            }
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

setup_image_deps() {
    # Skip if not installing image features at all
    if [ "$INSTALL_IMAGE" = "0" ]; then
        return
    fi

    print_step "Installing image processing dependencies..."

    # Always install face detection/swap dependencies (needed for img2img with any backend)
    # These include: onnxruntime, insightface, opencv, mkl (for Intel systems)
    echo "  Installing face detection dependencies (InsightFace, MKL)..."
    pip install onnxruntime huggingface_hub insightface opencv-python-headless mkl -q
    print_success "Face detection dependencies installed"

    # Skip diffusers if using ComfyUI backend
    if [ "$IMAGE_BACKEND" != "native" ]; then
        print_success "Using ComfyUI backend - skipping diffusers installation"
        return
    fi

    print_step "Installing native image generation dependencies..."

    # Install diffusers/transformers/accelerate for native backend
    pip install diffusers transformers accelerate safetensors -q
    print_success "Diffusers installed"

    # Install GPU-specific PyTorch if needed
    case "$GPU_TYPE" in
        nvidia)
            # Check if torch with CUDA is already installed
            if ! python3 -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
                echo "  Installing PyTorch with CUDA support..."
                pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121 -q
            fi
            print_success "PyTorch CUDA ready"
            ;;
        intel)
            # Intel XPU requires specific torch version
            echo "  Intel XPU: Using existing IPEX PyTorch from venv-ipex"
            print_success "PyTorch XPU ready"
            ;;
        amd)
            # Check for ROCm PyTorch
            if ! python3 -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
                echo ""
                print_warning "Installing PyTorch with ROCm support..."
                echo "  This downloads ~5GB and may take 5-15 minutes."
                echo "  Using ROCm 7.0 nightly (required for Python 3.13+)"
                echo ""

                # Check disk space
                AVAIL_GB=$(df -BG "$HOME" | awk 'NR==2 {print $4}' | tr -d 'G')
                if [ "$AVAIL_GB" -lt 10 ]; then
                    print_error "Less than 10GB free disk space. Need ~10GB for PyTorch ROCm."
                    echo "  Free up space and try again."
                    exit 1
                fi

                # Use home dir for temp to avoid small /tmp (zram) issues
                mkdir -p "$HOME/tmp"
                TMPDIR="$HOME/tmp" TEMP="$HOME/tmp" TMP="$HOME/tmp" \
                    pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/nightly/rocm7.0 || {
                        print_error "PyTorch ROCm installation failed!"
                        echo ""
                        echo "  Common issues:"
                        echo "  • Not enough disk space (need ~10GB)"
                        echo "  • Network timeout (try again)"
                        echo "  • Python version not supported (need 3.10-3.13)"
                        echo ""
                        echo "  Manual install:"
                        echo "  TMPDIR=~/tmp pip install torch torchvision --index-url https://download.pytorch.org/whl/nightly/rocm7.0"
                        rm -rf "$HOME/tmp"
                        exit 1
                    }
                rm -rf "$HOME/tmp"

                # Verify installation
                if python3 -c "import torch; assert torch.cuda.is_available()" 2>/dev/null; then
                    GFX_VER=$(python3 -c "import torch; print(torch.cuda.get_device_name(0))" 2>/dev/null || echo "Unknown")
                    print_success "PyTorch ROCm installed: $GFX_VER"
                else
                    print_warning "PyTorch installed but GPU not detected. Check ROCm installation."
                fi
            else
                print_success "PyTorch ROCm already installed"
            fi
            ;;
        *)
            # CPU fallback
            if ! python3 -c "import torch" 2>/dev/null; then
                echo "  Installing PyTorch (CPU)..."
                pip install torch torchvision -q
            fi
            print_success "PyTorch CPU ready"
            ;;
    esac

    # Optional: Install xformers for faster attention (NVIDIA only)
    if [ "$GPU_TYPE" = "nvidia" ]; then
        read -p "Install xformers for faster image generation? [Y/n]: " INSTALL_XFORMERS
        INSTALL_XFORMERS=${INSTALL_XFORMERS:-Y}
        if [[ "$INSTALL_XFORMERS" =~ ^[Yy] ]]; then
            echo "  Installing xformers..."
            pip install xformers -q 2>/dev/null || print_warning "xformers install failed (optional)"
        fi
    fi

    print_success "Image generation dependencies installed"
}

setup_xpu_image_instance() {
    # Only offer for Intel Arc systems using IPEX-LLM for chat
    if [ "$BACKEND" != "intel" ] || [ "$IMAGE_BACKEND" != "native" ]; then
        return
    fi

    echo ""
    print_step "Intel Arc Dual-Instance Setup"
    echo ""
    echo "  You're using IPEX-LLM for chat on Intel Arc."
    echo "  For image generation, you can run a separate instance using PyTorch XPU."
    echo ""
    echo "  This sets up:"
    echo "    - Port 3051: Chat (IPEX-LLM) - current instance"
    echo "    - Port 3052: Images (PyTorch XPU) - separate instance"
    echo ""
    read -p "Set up XPU image generation instance? [y/N]: " SETUP_XPU_IMAGE
    SETUP_XPU_IMAGE=${SETUP_XPU_IMAGE:-N}

    if [[ ! "$SETUP_XPU_IMAGE" =~ ^[Yy] ]]; then
        print_warning "Skipping XPU image instance setup"
        echo "  You can set this up later with: ./scripts/setup-image-instance.sh"
        return
    fi

    print_step "Setting up XPU image instance..."

    # Create venv-xpu
    if [ ! -d "$SCRIPT_DIR/venv-xpu" ]; then
        echo "  Creating venv-xpu..."
        python3 -m venv "$SCRIPT_DIR/venv-xpu"
    fi

    # Install PyTorch XPU
    echo "  Installing PyTorch XPU (this may take a few minutes)..."
    source "$SCRIPT_DIR/venv-xpu/bin/activate"
    pip install --upgrade pip -q
    pip install torch torchvision --index-url https://download.pytorch.org/whl/test/xpu -q
    pip install diffusers transformers accelerate safetensors -q
    pip install fastapi uvicorn sqlalchemy python-jose passlib bcrypt python-multipart httpx aiofiles pillow pydantic edge-tts -q
    deactivate

    # Run setup script
    chmod +x "$SCRIPT_DIR/scripts/setup-image-instance.sh"
    "$SCRIPT_DIR/scripts/setup-image-instance.sh"

    # Install systemd service for XPU image instance
    if [ -d "$HOME/.config/systemd/user" ]; then
        cp "$SCRIPT_DIR/posterchanai-xpu-image.service" "$HOME/.config/systemd/user/"
        systemctl --user daemon-reload
        echo ""
        print_success "XPU image instance installed!"
        echo ""
        echo "  To enable and start:"
        echo "    systemctl --user enable posterchanai-xpu-image"
        echo "    systemctl --user start posterchanai-xpu-image"
        echo ""
        echo "  Then configure the main instance (Admin > Site Settings > Load Balancing):"
        echo "    Image Server URLs: http://localhost:3052"
        XPU_IMAGE_INSTALLED="1"
    else
        print_warning "User systemd not available, manual setup required"
        echo "  See: $SCRIPT_DIR/posterchanai-xpu-image.service"
    fi
}

setup_systemd() {
    # Set SERVICE_NAME early so print_summary can use it even if systemd is skipped
    SERVICE_NAME="posterchanai"
    if [ "$BACKEND" = "intel" ]; then
        SERVICE_NAME="posterchanai-ipex"
    elif [ "$BACKEND" = "amd" ]; then
        SERVICE_NAME="posterchanai-rocm"
    fi

    print_step "Configure systemd service?"
    read -p "Install as systemd service? [Y/n]: " INSTALL_SERVICE
    INSTALL_SERVICE=${INSTALL_SERVICE:-Y}

    if [[ ! "$INSTALL_SERVICE" =~ ^[Yy] ]]; then
        print_warning "Skipping systemd setup"
        return
    fi

    print_step "Creating systemd service: $SERVICE_NAME"

    # Create run script for the backend
    RUN_SCRIPT="$SCRIPT_DIR/run-$BACKEND.sh"

    case "$BACKEND" in
        intel)
            # Detect oneAPI version for the run script
            ONEAPI_VER=""
            if [ -d /opt/intel/oneapi/2025.0 ]; then
                ONEAPI_VER="2025.0"
            elif [ -d /opt/intel/oneapi/2024.2 ]; then
                ONEAPI_VER="2024.2"
            fi

            cat > "$RUN_SCRIPT" << 'SCRIPT'
#!/bin/bash
# IPEX-LLM wrapper script for Intel Arc GPU
# Sets up the environment and runs with executable stack enabled

# Detect oneAPI installation path
ONEAPI_ROOT=""
if [ -d /opt/intel/oneapi/2025.0 ]; then
    ONEAPI_ROOT="/opt/intel/oneapi/2025.0"
elif [ -d /opt/intel/oneapi/2024.2 ]; then
    ONEAPI_ROOT="/opt/intel/oneapi/2024.2"
elif [ -d /opt/intel/oneapi ]; then
    ONEAPI_ROOT="/opt/intel/oneapi"
fi

if [ -z "$ONEAPI_ROOT" ]; then
    echo "ERROR: Intel oneAPI not found in /opt/intel/oneapi" >&2
    exit 1
fi

# Set Intel oneAPI environment explicitly
# This is more reliable than 'source oneapi-vars.sh' in systemd contexts
export ONEAPI_ROOT
# Include venv-ipex/lib for MKL libraries (required for InsightFace face detection)
export LD_LIBRARY_PATH="$SCRIPT_DIR/venv-ipex/lib:$ONEAPI_ROOT/lib:${LD_LIBRARY_PATH:-/usr/local/lib}"
export PATH="$ONEAPI_ROOT/bin:$PATH"
export OCL_ICD_FILENAMES="$ONEAPI_ROOT/lib/libintelocl.so"

# Also source the vars script for any additional setup
if [ -f "$ONEAPI_ROOT/oneapi-vars.sh" ]; then
    source "$ONEAPI_ROOT/oneapi-vars.sh" --force 2>/dev/null || true
elif [ -f "$ONEAPI_ROOT/setvars.sh" ]; then
    source "$ONEAPI_ROOT/setvars.sh" --force 2>/dev/null || true
fi

# Preload VTune stub if available (suppresses symbol warnings)
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

# Help with CUDA memory fragmentation for image generation
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

exec "$SCRIPT_DIR/venv/bin/python" run.py "$@"
SCRIPT
            ;;

        amd)
            # Get GFX version for HSA override
            GFX_VER=""
            if command -v rocminfo &>/dev/null; then
                GFX_VER=$(rocminfo 2>/dev/null | grep -o 'gfx[0-9]*' | head -1 | sed 's/gfx//')
                # Convert gfx1030 -> 10.3.0, gfx1100 -> 11.0.0, etc.
                if [ -n "$GFX_VER" ]; then
                    MAJOR=${GFX_VER:0:2}
                    MINOR=${GFX_VER:2:1}
                    PATCH=${GFX_VER:3:1}
                    GFX_VER="${MAJOR#0}.${MINOR}.${PATCH:-0}"
                fi
            fi

            cat > "$RUN_SCRIPT" << SCRIPT
#!/bin/bash
# AMD ROCm wrapper script
SCRIPT_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
cd "\$SCRIPT_DIR"

# ROCm environment
export ROCM_PATH=/opt/rocm
export HIP_PATH=/opt/rocm
export LD_LIBRARY_PATH=/opt/rocm/lib:\$LD_LIBRARY_PATH
export PATH=/opt/rocm/bin:\$PATH

# GPU architecture override (adjust if needed for your GPU)
# Common values: 10.3.0 (RX 6000), 11.0.0 (RX 7000)
export HSA_OVERRIDE_GFX_VERSION=${GFX_VER:-10.3.0}

# Help with memory management
export PYTORCH_HIP_ALLOC_CONF=expandable_segments:True

exec "\$SCRIPT_DIR/venv/bin/python" run.py "\$@"
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

    # Detect oneAPI path for Intel backend
    ONEAPI_PATH=""
    if [ "$BACKEND" = "intel" ]; then
        if [ -d /opt/intel/oneapi/2025.0 ]; then
            ONEAPI_PATH="/opt/intel/oneapi/2025.0"
        elif [ -d /opt/intel/oneapi/2024.2 ]; then
            ONEAPI_PATH="/opt/intel/oneapi/2024.2"
        elif [ -d /opt/intel/oneapi ]; then
            ONEAPI_PATH="/opt/intel/oneapi"
        fi
    fi

    if [ "$BACKEND" = "intel" ] && [ -n "$ONEAPI_PATH" ]; then
        # Intel backend needs explicit oneAPI environment variables
        sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null << EOF
[Unit]
Description=Posterchan AI ($BACKEND backend)
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$SCRIPT_DIR

# Python virtual environment
Environment="PATH=$VENV_PATH/bin:$ONEAPI_PATH/bin:/usr/local/bin:/usr/bin"
Environment="VIRTUAL_ENV=$VENV_PATH"

# Intel oneAPI libraries - CRITICAL for SYCL/llama.cpp
# These must be set explicitly since 'source oneapi-vars.sh' doesn't work in systemd
# Include venv-ipex/lib for MKL libraries (required for InsightFace face detection)
Environment="LD_LIBRARY_PATH=$VENV_PATH/lib:$ONEAPI_PATH/lib:/usr/local/lib"
Environment="OCL_ICD_FILENAMES=$ONEAPI_PATH/lib/libintelocl.so"
Environment="ONEAPI_ROOT=$ONEAPI_PATH"

# IPEX-LLM optimizations
Environment="ENABLE_SDP_FUSION=1"
Environment="SYCL_CACHE_PERSISTENT=1"
Environment="BIGDL_LLM_XMX_DISABLED=1"
Environment="ZES_ENABLE_SYSMAN=1"
Environment="TORCH_DEVICE_BACKEND_AUTOLOAD=0"

# Preload VTune stub to suppress symbol warnings
Environment="LD_PRELOAD=/usr/local/lib/libittnotify.so"

ExecStart=$RUN_SCRIPT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    else
        # Non-Intel backends use simple service file
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
    fi

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
    echo "  • Qwen3-8B-abliterated (5.9GB) - Fast, uncensored, good quality"
    echo "  • Qwen2.5-7B-Instruct (7GB) - Fast, good quality"
    echo "  • Mistral-7B-Instruct (7GB) - Great all-rounder"
    echo ""

    read -p "Download a starter model? [y/N]: " DOWNLOAD_MODEL

    if [[ "$DOWNLOAD_MODEL" =~ ^[Yy] ]]; then
        MODELS_PATH="/var/lib/posterchanai/models"
        echo ""
        echo "  Downloading Qwen3-8B-abliterated Q5_K_M..."

        MODEL_URL="https://huggingface.co/DevQuasar/huihui-ai.Qwen3-8B-abliterated-GGUF/resolve/main/huihui-ai.Qwen3-8B-abliterated.Q5_K_M.gguf"
        MODEL_FILE="$MODELS_PATH/Qwen3-8B-abliterated-Q5_K_M.gguf"

        DOWNLOAD_OK=0
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
            echo ""
            echo "  Configure this model in Admin Settings > LLM Model Path"
        elif [ -f "$MODEL_FILE" ]; then
            rm -f "$MODEL_FILE"
            print_error "Download failed or incomplete. Please try again manually."
            echo "  URL: $MODEL_URL"
        fi
    fi
}

print_summary() {
    echo ""
    echo -e "${GREEN}╔═══════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  ${BOLD}Installation Complete!${NC}${GREEN}                                      ║${NC}"
    echo -e "${GREEN}╚═══════════════════════════════════════════════════════════════╝${NC}"
    echo ""

    # Show component info
    echo -e "  ${BOLD}Components:${NC}"
    if [ "$INSTALL_LLM" = "1" ]; then
        echo "    LLM Backend: $LLM_BACKEND"
    else
        echo "    LLM Backend: external (Ollama)"
    fi
    if [ "$INSTALL_IMAGE" = "1" ]; then
        echo "    Image Backend: $IMAGE_BACKEND"
    else
        echo "    Image Backend: external (ComfyUI)"
    fi
    echo ""

    echo "  Service: $SERVICE_NAME"
    echo ""
    echo -e "  ${BOLD}Access:${NC} http://localhost:3051"
    echo -e "  ${BOLD}Login:${NC}  admin / admin"
    echo ""
    echo -e "  ${BOLD}Commands:${NC}"
    echo "    Start:   sudo systemctl start $SERVICE_NAME"
    echo "    Stop:    sudo systemctl stop $SERVICE_NAME"
    echo "    Logs:    sudo journalctl -u $SERVICE_NAME -f"
    echo ""

    # Show next steps based on configuration
    echo -e "  ${BOLD}Next Steps:${NC}"
    if [ "$INSTALL_LLM" = "1" ] && [ "$LLM_BACKEND" != "ollama" ]; then
        echo "    1. Download a GGUF model to /var/lib/posterchanai/models/"
        echo "    2. Configure model path in Admin > AI Settings"
    else
        echo "    1. Configure Ollama URL in Admin > AI Settings"
    fi

    if [ "$INSTALL_IMAGE" = "1" ] && [ "$IMAGE_BACKEND" = "native" ]; then
        echo "    • Download a Stable Diffusion model (SDXL recommended)"
        echo "    • Configure image model in Admin > Image Generation"
        echo "    • Tip: Use an anime model (e.g., Animagine) for anime-style prompts"
    elif [ "$IMAGE_BACKEND" = "comfyui" ]; then
        echo "    • Configure ComfyUI URL in Admin > Image Generation"
        echo "    • Set both Default Model and Anime Model for auto-switching"
    fi
    echo ""
    echo -e "  ${BOLD}RAG (Codebase Indexing):${NC}"
    echo "    • Create collections in Admin > RAG tab"
    echo "    • Use VS Code extension for real-time sync"
    echo "    • First query downloads ~90MB embedding model"
    echo ""
}

# Handle --help and --packages options
if [ "$1" = "--help" ] || [ "$1" = "-h" ]; then
    echo "Posterchanai Installer"
    echo ""
    echo "Usage: ./install.sh [options]"
    echo ""
    echo "Options:"
    echo "  --help, -h       Show this help message"
    echo "  --packages       Show required packages for your distro"
    echo ""
    echo "Installation Types:"
    echo "  Full Stack       LLM + Image Generation (recommended)"
    echo "  LLM Only         Chat/text generation only"
    echo "  Image Only       Image generation only (use external LLM)"
    echo "  Lightweight      Web UI only (external Ollama + ComfyUI)"
    echo ""
    echo "LLM Backends:"
    echo "  Intel Arc        IPEX-LLM + llama.cpp SYCL"
    echo "  NVIDIA           llama.cpp CUDA"
    echo "  AMD              llama.cpp ROCm/HIP"
    echo "  CPU              llama.cpp (slow)"
    echo "  Ollama           External Ollama service"
    echo ""
    echo "Image Backends:"
    echo "  Native           Built-in diffusers (CUDA/XPU/ROCm/CPU)"
    echo "  ComfyUI          External ComfyUI service"
    echo ""
    echo "RAG (Codebase Indexing):"
    echo "  Built-in         ChromaDB + sentence-transformers"
    echo "                   First query downloads ~90MB embedding model"
    echo ""
    exit 0
fi

if [ "$1" = "--packages" ]; then
    print_banner
    detect_distro
    echo -e "${BOLD}Required packages for your system:${NC}"
    echo ""
    show_install_instructions
    exit 0
fi

# Main
print_banner
check_dependencies
detect_gpu
select_components
select_backend
select_image_backend
setup_directories
setup_python_env

# Install LLM dependencies if selected and not using Ollama
if [ "$INSTALL_LLM" = "1" ] && [ "$LLM_BACKEND" != "ollama" ]; then
    setup_llama_cpp
fi

# Install image generation dependencies if selected
setup_image_deps

# Offer XPU image instance for Intel Arc users
setup_xpu_image_instance

setup_systemd

# Only offer model download if installing local LLM
if [ "$INSTALL_LLM" = "1" ] && [ "$LLM_BACKEND" != "ollama" ]; then
    download_model
fi

print_summary
