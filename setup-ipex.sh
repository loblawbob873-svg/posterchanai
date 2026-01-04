#!/bin/bash
# Setup script for IPEX-LLM backend with Intel Arc GPU support

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Setting up Posterchanai with IPEX-LLM backend..."

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
echo "If you see 'cannot enable executable stack' error:"
echo "  This is a glibc 2.41+ security feature. The run-ipex.sh script"
echo "  uses 'setarch -X' to work around it. If it still fails, you may need"
echo "  to use patchelf to clear the executable stack flag on the library:"
echo "    patchelf --clear-execstack venv-ipex/lib/python*/site-packages/intel_extension_for_pytorch/lib/libintel-ext-pt-cpu.so"
echo ""
echo "If you see 'NumPy 1.x cannot run in NumPy 2.x' error:"
echo "  Run: venv-ipex/bin/pip install 'numpy<2'"
echo ""
echo "If IPEX-LLM falls back to CPU (standard llama-cpp-python):"
echo "  Check that Intel oneAPI is properly installed and sourced."
echo "  The run-ipex.sh script should handle this automatically."
