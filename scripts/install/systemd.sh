#!/bin/bash
# Systemd Service Setup
# Sourced by install.sh

# Global variable for service name
SERVICE_NAME="posterchanai"

setup_systemd() {
    # Set SERVICE_NAME based on backend
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

    # Create run script
    create_run_script

    # Create systemd service file
    create_service_file

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

create_run_script() {
    local RUN_SCRIPT="$SCRIPT_DIR/run-$BACKEND.sh"

    case "$BACKEND" in
        intel)
            create_intel_run_script "$RUN_SCRIPT"
            ;;
        nvidia)
            create_nvidia_run_script "$RUN_SCRIPT"
            ;;
        amd)
            create_amd_run_script "$RUN_SCRIPT"
            ;;
        *)
            create_default_run_script "$RUN_SCRIPT"
            ;;
    esac

    chmod +x "$RUN_SCRIPT"
}

create_intel_run_script() {
    local RUN_SCRIPT="$1"
    cat > "$RUN_SCRIPT" << 'SCRIPT'
#!/bin/bash
# IPEX-LLM wrapper script for Intel Arc GPU
# This script sets up the complete Intel oneAPI environment for IPEX-LLM

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Detect oneAPI installation path
ONEAPI_ROOT=""
for dir in /opt/intel/oneapi/2025.0 /opt/intel/oneapi/2024.2 /opt/intel/oneapi; do
    if [ -d "$dir" ]; then
        ONEAPI_ROOT="$dir"
        break
    fi
done

if [ -z "$ONEAPI_ROOT" ]; then
    echo "ERROR: Intel oneAPI not found in /opt/intel/oneapi" >&2
    echo "Please install Intel oneAPI Base Toolkit first." >&2
    exit 1
fi

export ONEAPI_ROOT

# Build comprehensive LD_LIBRARY_PATH with all oneAPI components
# This is critical - IPEX-LLM needs libraries from multiple subdirectories
ONEAPI_LIBS=""
for subdir in lib compiler/lib mkl/lib tbb/lib ccl/lib; do
    libpath="$ONEAPI_ROOT/$subdir"
    if [ -d "$libpath" ]; then
        ONEAPI_LIBS="${ONEAPI_LIBS:+$ONEAPI_LIBS:}$libpath"
    fi
done

# Also check for versioned compiler directories (e.g., compiler/2025.0/lib)
for compiler_dir in "$ONEAPI_ROOT"/compiler/*/lib; do
    if [ -d "$compiler_dir" ]; then
        ONEAPI_LIBS="${ONEAPI_LIBS:+$ONEAPI_LIBS:}$compiler_dir"
    fi
done

# Add venv libs (for MKL from pip) and system libs
export LD_LIBRARY_PATH="$SCRIPT_DIR/venv-ipex/lib:$ONEAPI_LIBS:/usr/local/lib:${LD_LIBRARY_PATH:-}"

# Set PATH to include oneAPI binaries
export PATH="$ONEAPI_ROOT/bin:$SCRIPT_DIR/venv-ipex/bin:$PATH"

# OpenCL ICD configuration
if [ -f "$ONEAPI_ROOT/lib/libintelocl.so" ]; then
    export OCL_ICD_FILENAMES="$ONEAPI_ROOT/lib/libintelocl.so"
fi

# Source oneAPI vars script for any additional setup (SYCL compiler paths, etc.)
# Use --force to override any existing environment
if [ -f "$ONEAPI_ROOT/oneapi-vars.sh" ]; then
    source "$ONEAPI_ROOT/oneapi-vars.sh" --force 2>/dev/null || true
elif [ -f "$ONEAPI_ROOT/setvars.sh" ]; then
    source "$ONEAPI_ROOT/setvars.sh" --force 2>/dev/null || true
fi

# Preload VTune stub if available (suppresses missing symbol warnings)
if [ -f /usr/local/lib/libittnotify.so ]; then
    export LD_PRELOAD=/usr/local/lib/libittnotify.so
fi

# IPEX-LLM optimizations for Intel Arc GPU
export ENABLE_SDP_FUSION=1
export SYCL_CACHE_PERSISTENT=1
export BIGDL_LLM_XMX_DISABLED=1
export ZES_ENABLE_SYSMAN=1
export TORCH_DEVICE_BACKEND_AUTOLOAD=0

# Intel GPU memory management
export NEOReadDebugKeys=1
export OverrideDefaultFP64Settings=1

# Log environment for debugging
echo "[IPEX] ONEAPI_ROOT=$ONEAPI_ROOT"
echo "[IPEX] LD_LIBRARY_PATH includes: $(echo $LD_LIBRARY_PATH | tr ':' '\n' | head -5 | tr '\n' ' ')..."

cd "$SCRIPT_DIR"

# Use setarch to allow executable stack (required by some IPEX components on modern glibc)
exec setarch $(uname -m) -X "$SCRIPT_DIR/venv-ipex/bin/python" run.py "$@"
SCRIPT
}

create_nvidia_run_script() {
    local RUN_SCRIPT="$1"
    cat > "$RUN_SCRIPT" << 'SCRIPT'
#!/bin/bash
# NVIDIA CUDA wrapper script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Help with CUDA memory fragmentation
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

exec "$SCRIPT_DIR/venv/bin/python" run.py "$@"
SCRIPT
}

create_amd_run_script() {
    local RUN_SCRIPT="$1"
    local GFX_VER=""

    # Get GFX version for HSA override
    if command -v rocminfo &>/dev/null; then
        GFX_VER=$(rocminfo 2>/dev/null | grep -o 'gfx[0-9]*' | head -1 | sed 's/gfx//')
        if [ -n "$GFX_VER" ]; then
            local MAJOR=${GFX_VER:0:2}
            local MINOR=${GFX_VER:2:1}
            local PATCH=${GFX_VER:3:1}
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

# GPU architecture override
export HSA_OVERRIDE_GFX_VERSION=${GFX_VER:-10.3.0}

# Help with memory management
export PYTORCH_HIP_ALLOC_CONF=expandable_segments:True

exec "\$SCRIPT_DIR/venv/bin/python" run.py "\$@"
SCRIPT
}

create_default_run_script() {
    local RUN_SCRIPT="$1"
    cat > "$RUN_SCRIPT" << 'SCRIPT'
#!/bin/bash
# CPU/Ollama wrapper script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
exec "$SCRIPT_DIR/venv/bin/python" run.py "$@"
SCRIPT
}

create_service_file() {
    local VENV_PATH="$SCRIPT_DIR/venv"
    [ "$BACKEND" = "intel" ] && VENV_PATH="$SCRIPT_DIR/venv-ipex"

    local RUN_SCRIPT="$SCRIPT_DIR/run-$BACKEND.sh"

    if [ "$BACKEND" = "intel" ]; then
        local ONEAPI_PATH
        ONEAPI_PATH=$(detect_oneapi_root)
        create_intel_service_file "$VENV_PATH" "$RUN_SCRIPT" "$ONEAPI_PATH"
    else
        create_simple_service_file "$VENV_PATH" "$RUN_SCRIPT"
    fi
}

create_intel_service_file() {
    local VENV_PATH="$1"
    local RUN_SCRIPT="$2"
    local ONEAPI_PATH="$3"

    # The run script handles all environment setup (oneAPI, IPEX optimizations, etc.)
    # We only set minimal environment here to ensure the script can find basic tools
    sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null << EOF
[Unit]
Description=Posterchan AI (IPEX-LLM Intel Arc GPU)
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$SCRIPT_DIR

# Minimal environment - the run script sets up oneAPI and IPEX environment
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
Environment="HOME=$HOME"

# The run script (run-intel.sh) handles:
# - Detecting and configuring Intel oneAPI
# - Setting LD_LIBRARY_PATH with all required library paths
# - IPEX-LLM optimization environment variables
# - Sourcing oneapi-vars.sh for SYCL compiler support
ExecStart=$RUN_SCRIPT

Restart=always
RestartSec=5

# Give extra time for model loading on restart
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF
}

create_simple_service_file() {
    local VENV_PATH="$1"
    local RUN_SCRIPT="$2"

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
}
