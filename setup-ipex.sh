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

# Install base requirements (without llama-cpp-python)
echo "Installing base dependencies..."
pip install -r requirements.txt

# Install llama-cpp-python with SYCL support for Intel GPUs
echo ""
echo "Installing llama-cpp-python with Intel GPU support..."
echo "This may take several minutes..."

# Set build flags for Intel SYCL backend
export CMAKE_ARGS="-DGGML_SYCL=ON -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx"

pip install llama-cpp-python --force-reinstall --no-cache-dir

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
