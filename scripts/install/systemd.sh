#!/bin/bash
# Systemd Service Setup
# Sourced by install.sh

# Global variable for service name
SERVICE_NAME="posterchanai"

setup_systemd() {
    # Set SERVICE_NAME based on backend. Intel uses the plain "posterchanai" name (its
    # Intel-specific environment lives entirely in run-intel.sh, not the unit name); only AMD
    # keeps a suffix to coexist with a ROCm-specific launcher.
    SERVICE_NAME="posterchanai"
    if [ "$BACKEND" = "amd" ]; then
        SERVICE_NAME="posterchanai-rocm"
    fi

    print_step "Configure systemd service?"
    read -p "Install as systemd service? [Y/n]: " INSTALL_SERVICE
    INSTALL_SERVICE=${INSTALL_SERVICE:-Y}

    if [[ ! "$INSTALL_SERVICE" =~ ^[Yy] ]]; then
        print_warning "Skipping systemd setup"
        return
    fi

    # Intel Arc with native image gen gets TWO services (chat + image)
    if [ "$BACKEND" = "intel" ] && [ "$IMAGE_BACKEND" = "native" ]; then
        setup_intel_dual_services
    else
        setup_single_service
    fi
}

setup_single_service() {
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

setup_intel_dual_services() {
    echo ""
    print_step "Creating Intel Arc dual-service setup..."
    echo "  Chat service:  $SERVICE_NAME.service (port 3051)"
    echo "  Image service: posterchanai-xpu-image.service (port 3052)"
    echo ""

    # Create chat service (IPEX-LLM)
    print_step "Creating chat service: $SERVICE_NAME"
    create_run_script
    create_service_file
    print_success "Created $SERVICE_NAME.service"

    # Create image service (XPU)
    print_step "Creating image service: posterchanai-xpu-image"
    create_xpu_image_run_script
    create_xpu_image_service_file
    print_success "Created posterchanai-xpu-image.service"

    sudo systemctl daemon-reload
    print_success "Both systemd services created"

    read -p "Enable and start both services now? [Y/n]: " START_NOW
    START_NOW=${START_NOW:-Y}

    if [[ "$START_NOW" =~ ^[Yy] ]]; then
        # Start chat service
        sudo systemctl enable $SERVICE_NAME
        sudo systemctl start $SERVICE_NAME
        sleep 3
        if systemctl is-active --quiet $SERVICE_NAME; then
            print_success "Chat service ($SERVICE_NAME) started on port 3051"
        else
            print_error "Chat service failed. Check: sudo journalctl -u $SERVICE_NAME -n 50"
        fi

        # Start image service
        sudo systemctl enable posterchanai-xpu-image
        sudo systemctl start posterchanai-xpu-image
        sleep 3
        if systemctl is-active --quiet posterchanai-xpu-image; then
            print_success "Image service (posterchanai-xpu-image) started on port 3052"
        else
            print_error "Image service failed. Check: sudo journalctl -u posterchanai-xpu-image -n 50"
        fi
    fi

    echo ""
    print_step "Configure image load balancing"
    echo "  To use the image service, add http://localhost:3052 to:"
    echo "  Admin Settings > Image > Image Server URLs"
    echo ""
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
# Sets up the environment and runs with executable stack enabled

# Get script directory first (needed for LD_LIBRARY_PATH)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Detect oneAPI installation paths - need both 2024.2 and 2025.0 for compatibility
# IPEX-LLM is built against MKL 2024.2 (libmkl_sycl_blas.so.4)
# IMPORTANT: Must include compiler/VERSION/lib for libsycl.so runtime
ONEAPI_ROOT=""
ONEAPI_LIB_PATHS=""

if [ -d /opt/intel/oneapi/2025.0 ]; then
    ONEAPI_ROOT="/opt/intel/oneapi/2025.0"
    # Include compiler lib path for libsycl.so.8
    ONEAPI_LIB_PATHS="/opt/intel/oneapi/compiler/2025.0/lib:/opt/intel/oneapi/2025.0/lib"
fi

# Add 2024.2 libs for IPEX compatibility (needs .so.4 versions)
if [ -d /opt/intel/oneapi/2024.2 ]; then
    [ -z "$ONEAPI_ROOT" ] && ONEAPI_ROOT="/opt/intel/oneapi/2024.2"
    # Include both compiler and unified lib paths
    ONEAPI_LIB_PATHS="${ONEAPI_LIB_PATHS:+$ONEAPI_LIB_PATHS:}/opt/intel/oneapi/compiler/2024.2/lib:/opt/intel/oneapi/2024.2/lib"
fi

if [ -z "$ONEAPI_ROOT" ] && [ -d /opt/intel/oneapi ]; then
    ONEAPI_ROOT="/opt/intel/oneapi"
    ONEAPI_LIB_PATHS="/opt/intel/oneapi/compiler/latest/lib:/opt/intel/oneapi/lib"
fi

if [ -z "$ONEAPI_ROOT" ]; then
    echo "ERROR: Intel oneAPI not found in /opt/intel/oneapi" >&2
    exit 1
fi

# Set Intel oneAPI environment explicitly
# This is more reliable than 'source oneapi-vars.sh' in systemd contexts
export ONEAPI_ROOT
# Include venv-ipex/lib for MKL libraries (required for InsightFace face detection)
# Include both oneAPI versions for library compatibility
export LD_LIBRARY_PATH="$SCRIPT_DIR/venv-ipex/lib:$ONEAPI_LIB_PATHS:${LD_LIBRARY_PATH:-/usr/local/lib}"
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

cd "$SCRIPT_DIR"
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

# Persist MIOpen's compiled conv kernels so image gen pays the ROCm cold-start
# (kernel compilation) ONCE, not on every restart — the big AMD image-gen perf
# fix. FIND_MODE=2 (FAST) shortens the first-run kernel search.
export MIOPEN_USER_DB_PATH="\$SCRIPT_DIR/data/miopen"
export MIOPEN_CUSTOM_CACHE_DIR="\$SCRIPT_DIR/data/miopen"
export MIOPEN_FIND_MODE=2
mkdir -p "\$MIOPEN_USER_DB_PATH"

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

# Include venv so yt-dlp, ffmpeg and other pip-installed binaries are found
Environment="PATH=$VENV_PATH/bin:/usr/local/bin:/usr/bin:/bin"
Environment="HOME=$HOME"

# The run script (run-intel.sh) handles:
# - Detecting and configuring Intel oneAPI
# - Setting LD_LIBRARY_PATH with all required library paths
# - IPEX-LLM optimization environment variables
# - Sourcing oneapi-vars.sh for SYCL compiler support
ExecStart=$RUN_SCRIPT

# Quick restart - don't wait forever for stuck GPU processes
TimeoutStopSec=10
KillMode=mixed
KillSignal=SIGTERM
SendSIGKILL=yes

Restart=always
RestartSec=3

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

# Quick restart - don't wait forever for stuck GPU processes
TimeoutStopSec=10
KillMode=mixed
KillSignal=SIGTERM
SendSIGKILL=yes

Restart=always
RestartSec=3
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF
}

# Intel Arc XPU Image Service Functions

create_xpu_image_run_script() {
    local RUN_SCRIPT="$SCRIPT_DIR/run-xpu-image.sh"

    cat > "$RUN_SCRIPT" << 'SCRIPT'
#!/bin/bash
# PyTorch XPU image-gen wrapper for Intel Arc.
# Modern stack: torch 2.8 XPU, which BUNDLES its own oneAPI runtime (pip: intel-sycl-rt/
# dpcpp-cpp-rt/mkl/umf). Do NOT source a system oneAPI here - it conflicts with the bundled
# runtime. SDXL at >=768 needs system IGC >= 2.35.5 (older IGC fails oneDNN "could not create a
# primitive"). Prefer venv-xpu-new (modern); fall back to legacy venv-xpu + oneAPI 2025.0.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -d "$SCRIPT_DIR/venv-xpu-new" ]; then
    VENV="$SCRIPT_DIR/venv-xpu-new"
    export LD_LIBRARY_PATH="$VENV/lib:/usr/lib64:${LD_LIBRARY_PATH:-/usr/local/lib}"
    echo "[XPU-Image] venv=venv-xpu-new (torch 2.8 XPU, bundled oneAPI), system IGC"
elif "$SCRIPT_DIR/venv-xpu/bin/python" -c 'import torch,sys; v=tuple(int(x) for x in torch.__version__.split("+")[0].split(".")[:2]); sys.exit(0 if v>=(2,8) else 1)' 2>/dev/null; then
    # venv-xpu already holds torch >=2.8 (fresh modern install): no oneAPI sourcing.
    VENV="$SCRIPT_DIR/venv-xpu"
    export LD_LIBRARY_PATH="$VENV/lib:/usr/lib64:${LD_LIBRARY_PATH:-/usr/local/lib}"
    echo "[XPU-Image] venv=venv-xpu (torch 2.8 XPU, bundled oneAPI), system IGC"
else
    # Legacy fallback: torch 2.5 + system oneAPI 2025.0
    VENV="$SCRIPT_DIR/venv-xpu"
    ONEAPI_ROOT="/opt/intel/oneapi/2025.0"; [ -d "$ONEAPI_ROOT" ] || ONEAPI_ROOT="/opt/intel/oneapi"
    export ONEAPI_ROOT
    export LD_LIBRARY_PATH="$VENV/lib:$ONEAPI_ROOT/lib:$ONEAPI_ROOT/../compiler/2025.0/lib:${LD_LIBRARY_PATH:-/usr/local/lib}"
    export PATH="$ONEAPI_ROOT/bin:$PATH"
    [ -f "$ONEAPI_ROOT/oneapi-vars.sh" ] && source "$ONEAPI_ROOT/oneapi-vars.sh" --force 2>/dev/null || true
    echo "[XPU-Image] venv=venv-xpu (legacy torch 2.5 + oneAPI 2025.0)"
fi

export SYCL_CACHE_PERSISTENT=1
export ZES_ENABLE_SYSMAN=1
echo "[XPU-Image] Starting image service on port 3052"
cd "$SCRIPT_DIR"
exec "$VENV/bin/python" run.py --port 3052 "$@"
SCRIPT

    chmod +x "$RUN_SCRIPT"
}

create_xpu_image_service_file() {
    local VENV_PATH="$SCRIPT_DIR/venv-xpu"
    local RUN_SCRIPT="$SCRIPT_DIR/run-xpu-image.sh"

    sudo tee /etc/systemd/system/posterchanai-xpu-image.service > /dev/null << EOF
[Unit]
Description=Posterchan AI Image Generation (Intel XPU PyTorch)
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$SCRIPT_DIR

# Minimal environment - the run script sets up oneAPI environment
Environment="PATH=/usr/local/bin:/usr/bin:/bin"
Environment="HOME=$HOME"

# Use separate database and port for image-only instance
Environment="POSTERCHANAI_DB=posterchanai-image.db"
Environment="POSTERCHANAI_PORT=3052"

# The run script (run-xpu-image.sh) handles oneAPI setup
ExecStart=$RUN_SCRIPT

# Quick restart - don't wait forever for stuck GPU processes
TimeoutStopSec=10
KillMode=mixed
KillSignal=SIGTERM
SendSIGKILL=yes

Restart=always
RestartSec=3
TimeoutStartSec=120

[Install]
WantedBy=multi-user.target
EOF
}

