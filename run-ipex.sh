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
export LD_LIBRARY_PATH="$ONEAPI_ROOT/lib:${LD_LIBRARY_PATH:-/usr/local/lib}"
export PATH="$ONEAPI_ROOT/bin:$PATH"
export OCL_ICD_FILENAMES="$ONEAPI_ROOT/lib/libintelocl.so"

# Also source the vars script for any additional setup (if running interactively)
if [ -f "$ONEAPI_ROOT/oneapi-vars.sh" ]; then
    source "$ONEAPI_ROOT/oneapi-vars.sh" --force 2>/dev/null || true
elif [ -f "$ONEAPI_ROOT/setvars.sh" ]; then
    source "$ONEAPI_ROOT/setvars.sh" --force 2>/dev/null || true
fi

# Preload VTune stub library (suppresses symbol warnings)
[ -f /usr/local/lib/libittnotify.so ] && export LD_PRELOAD=/usr/local/lib/libittnotify.so

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
