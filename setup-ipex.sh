#!/bin/bash
# Setup script for IPEX-LLM backend with Intel Arc GPU support

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Setting up Posterchanai with IPEX-LLM backend..."

# Check for Level Zero (required for Intel GPU compute)
if ! ldconfig -p | grep -q libze_loader; then
    echo ""
    echo "WARNING: Level Zero (libze_loader) not found!"
    echo "This is required for Intel GPU acceleration with IPEX-LLM."
    echo ""
    if [ -f /etc/gentoo-release ]; then
        echo "Install with: sudo emerge -av dev-libs/level-zero"
    elif [ -f /etc/arch-release ]; then
        echo "Install with: sudo pacman -S level-zero-loader"
    elif [ -f /etc/debian_version ]; then
        echo "Install with: sudo apt install level-zero"
    else
        echo "Please install the level-zero package for your distribution."
    fi
    echo ""
    read -p "Continue anyway? [y/N]: " CONTINUE
    if [[ ! "$CONTINUE" =~ ^[Yy] ]]; then
        exit 1
    fi
fi

# Check for patchelf (needed to fix executable stack on modern glibc)
if ! command -v patchelf &>/dev/null; then
    echo ""
    echo "WARNING: patchelf not found!"
    echo "This is needed to fix IPEX libraries on systems with glibc 2.41+."
    echo ""
    if [ -f /etc/gentoo-release ]; then
        echo "Install with: sudo emerge -av dev-util/patchelf"
    elif [ -f /etc/arch-release ]; then
        echo "Install with: sudo pacman -S patchelf"
    elif [ -f /etc/debian_version ]; then
        echo "Install with: sudo apt install patchelf"
    else
        echo "Please install patchelf for your distribution."
    fi
    echo ""
    read -p "Continue anyway? [y/N]: " CONTINUE
    if [[ ! "$CONTINUE" =~ ^[Yy] ]]; then
        exit 1
    fi
fi

# Check for Intel oneAPI
ONEAPI_FOUND=0
if [ -f /opt/intel/oneapi/2025.0/oneapi-vars.sh ]; then
    source /opt/intel/oneapi/2025.0/oneapi-vars.sh
    ONEAPI_FOUND=1
elif [ -f /opt/intel/oneapi/2024.2/oneapi-vars.sh ]; then
    source /opt/intel/oneapi/2024.2/oneapi-vars.sh
    ONEAPI_FOUND=1
elif [ -f /opt/intel/oneapi/setvars.sh ]; then
    source /opt/intel/oneapi/setvars.sh
    ONEAPI_FOUND=1
elif [ -f ~/intel/oneapi/setvars.sh ]; then
    source ~/intel/oneapi/setvars.sh
    ONEAPI_FOUND=1
fi

if [ "$ONEAPI_FOUND" -eq 0 ]; then
    echo "ERROR: Intel oneAPI not found!"
    echo "Please install Intel oneAPI Base Toolkit from:"
    echo "  https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit-download.html"
    exit 1
fi

echo "Intel oneAPI found."

# Create upload directory
UPLOAD_PATH="/var/lib/posterchanai"
if [ ! -d "$UPLOAD_PATH" ]; then
    echo "Creating upload directory at $UPLOAD_PATH..."
    sudo mkdir -p "$UPLOAD_PATH"
    sudo chown $(whoami):$(whoami) "$UPLOAD_PATH"
fi

# Create IPEX virtual environment
if [ ! -d "venv-ipex" ]; then
    echo "Creating IPEX virtual environment..."
    python3 -m venv venv-ipex
fi

# Activate virtual environment
echo "Activating IPEX virtual environment..."
source venv-ipex/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip

# CRITICAL: Pin numpy<2 - IPEX/PyTorch was compiled with numpy 1.x
# Using numpy 2.x causes "module compiled using NumPy 1.x cannot run in NumPy 2.x" errors
echo "Installing numpy<2 (required for IPEX compatibility)..."
pip install "numpy<2"

# Install base requirements (without llama-cpp-python)
echo "Installing base dependencies..."
pip install -r requirements.txt

# Ensure numpy<2 wasn't overwritten by requirements.txt
pip install "numpy<2" --quiet

# Install IPEX-LLM (Intel's optimized LLM backend)
echo ""
echo "Installing IPEX-LLM..."
pip install --pre --upgrade ipex-llm[cpp]

# Fix executable stack issue on modern glibc (2.41+)
# The IPEX library requires executable stack which is blocked by default
if command -v patchelf &>/dev/null; then
    echo ""
    echo "Fixing IPEX library executable stack flags..."
    IPEX_LIB=$(find venv-ipex/lib -name "libintel-ext-pt-cpu.so" 2>/dev/null | head -1)
    if [ -n "$IPEX_LIB" ]; then
        patchelf --clear-execstack "$IPEX_LIB" 2>/dev/null && echo "  Fixed: $IPEX_LIB" || echo "  Warning: Could not patch $IPEX_LIB"
    fi
    # Also fix any other libraries that might need it
    for lib in $(find venv-ipex/lib -name "*.so" -exec sh -c 'readelf -l "$1" 2>/dev/null | grep -q "RWE" && echo "$1"' _ {} \; 2>/dev/null); do
        patchelf --clear-execstack "$lib" 2>/dev/null && echo "  Fixed: $lib"
    done
fi

# Also install llama-cpp-python with SYCL support as fallback
echo ""
echo "Installing llama-cpp-python with Intel GPU support (fallback)..."
echo "This may take several minutes..."

# Set build flags for Intel SYCL backend
export CMAKE_ARGS="-DGGML_SYCL=ON -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx"

pip install llama-cpp-python --force-reinstall --no-cache-dir

# Pin numpy<2 one more time (ipex-llm may have pulled in numpy 2.x)
pip install "numpy<2" --quiet

echo ""
echo "Setup complete!"
echo ""
echo "To run the application:"
echo "  ./run-ipex.sh"
echo ""
echo "Or with systemd:"
echo "  sudo systemctl start posterchanai-ipex"
echo ""
echo "Default login: admin / admin"
echo "Access at: http://localhost:3051"
echo ""
echo "=============================================="
echo "TROUBLESHOOTING"
echo "=============================================="
echo ""
echo "If you see 'libze_loader.so.1: cannot open shared object' error:"
echo "  Install Level Zero: sudo emerge -av dev-libs/level-zero (Gentoo)"
echo "                      sudo apt install level-zero (Debian/Ubuntu)"
echo ""
echo "If you see 'cannot enable executable stack' error:"
echo "  The installer should have fixed this automatically with patchelf."
echo "  If it persists, manually run:"
echo "    patchelf --clear-execstack venv-ipex/lib/python*/site-packages/intel_extension_for_pytorch/lib/libintel-ext-pt-cpu.so"
echo ""
echo "If you see 'libmkl_sycl_blas.so.4: cannot open' error:"
echo "  You may have multiple oneAPI versions. Ensure oneAPI 2024.2 is installed"
echo "  or the run script includes both library paths."
echo ""
echo "If you see 'NumPy 1.x cannot run in NumPy 2.x' error:"
echo "  Run: venv-ipex/bin/pip install 'numpy<2'"
echo ""
echo "If IPEX-LLM falls back to CPU (standard llama-cpp-python):"
echo "  1. Check that Intel oneAPI is properly installed"
echo "  2. Ensure dev-libs/level-zero is installed"
echo "  3. Check journalctl -u posterchanai-ipex for specific errors"
echo ""
echo "Thinking-mode models (Qwen3-abliterated, DeepSeek-R1):"
echo "  The system detects these models and attempts to suppress thinking mode."
echo "  Success rate: ~50% with abliterated models (they're designed to ignore instructions)"
echo "  For best results:"
echo "    - Use standard Qwen2.5, Llama-3, or Mistral (100% reliable)"
echo "    - Or use non-abliterated Qwen3 which follows instructions better"
echo "    - Abliterated models work but may occasionally show thinking process"
