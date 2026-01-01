#!/bin/bash
# IPEX-LLM wrapper script for Intel Arc GPU
# Sets up the environment and runs with executable stack enabled

# Set Intel oneAPI environment (requires 2025.0+ for llama.cpp SYCL)
if [ -f /opt/intel/oneapi/2025.0/oneapi-vars.sh ]; then
    source /opt/intel/oneapi/2025.0/oneapi-vars.sh --force
elif [ -f /opt/intel/oneapi/2024.2/oneapi-vars.sh ]; then
    source /opt/intel/oneapi/2024.2/oneapi-vars.sh --force
elif [ -f /opt/intel/oneapi/setvars.sh ]; then
    source /opt/intel/oneapi/setvars.sh --force
fi

# Preload VTune stub library
export LD_PRELOAD=/usr/local/lib/libittnotify.so

# IPEX-LLM optimizations
export ENABLE_SDP_FUSION=1
export SYCL_CACHE_PERSISTENT=1
export BIGDL_LLM_XMX_DISABLED=1
export ZES_ENABLE_SYSMAN=1

# Workaround for GLIBC 2.41 executable stack issue
export TORCH_DEVICE_BACKEND_AUTOLOAD=0

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Activate venv and run with executable stack enabled
cd "$SCRIPT_DIR"
exec setarch $(uname -m) -X "$SCRIPT_DIR/venv-ipex/bin/python" run.py "$@"
