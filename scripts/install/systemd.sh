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

    # Intel Arc now runs chat (llama.cpp SYCL) + native image (diffusers torch-XPU) from ONE
    # unified venv in ONE process (run-intel.sh), so it uses the single-service path like every
    # other backend. (The old dual chat/image split was only needed because EOL IPEX-LLM and
    # torch-XPU couldn't share a process — see setup_intel_dual_services below, now unused.)
    setup_single_service
    offer_split_services
}

# The stack can run as ONE unit (the historical layout) or as five: the web app plus
# posterchanai-{relay,worker,media,bots}. Offered here because the choice only makes sense once the
# main unit exists — this script's job — and because doing it by hand is the one change you must make
# in BOTH halves or not at all.
#
# Under the single unit the web app supervises the relay, mediamtx, pion-turn, tor and the bots, so
# restarting to ship a code change drops every connected Nostr client, kills live streams
# MID-BROADCAST, drops active calls and restarts the bots. Split, `sync.sh` restarts only what the
# deploy touched.
#
# DEFAULT IS NO. The single-unit layout is what every existing node runs and it keeps working; this is
# an opt-in improvement, not a migration anyone is forced through. Declining leaves POSTERCHANAI_ROLE
# unset, which means role 'all' — exactly the old behaviour.
offer_split_services() {
    local helper="$SCRIPT_DIR/scripts/install_services.sh"
    [ -x "$helper" ] || return 0
    # Only meaningful for the plain service name; a ROCm-suffixed unit is not what the helper edits.
    [ "$SERVICE_NAME" = "posterchanai" ] || return 0

    echo ""
    print_step "Run the relay, worker, media, tor, proxy and git host as SEPARATE services?"
    echo "  Recommended. Restarting the web app then no longer drops connected Nostr clients,"
    echo "  kills live streams mid-broadcast, drops calls, tears down Tor circuits, or kills"
    echo "  in-flight git clones/pushes."
    echo "  The BOT MANAGER stays with the app deliberately (Admin -> Bots drives it through an"
    echo "  in-process registry), so the app runs role \"app,bots\"."
    echo "  Reversible at any time with: scripts/install_services.sh --revert"
    read -p "Split into separate services? [y/N]: " SPLIT_SVC
    SPLIT_SVC=${SPLIT_SVC:-N}
    if [[ ! "$SPLIT_SVC" =~ ^[Yy] ]]; then
        print_warning "Keeping the single-service layout (role 'all')"
        return 0
    fi
    if "$helper" --launcher "run-$BACKEND.sh"; then
        print_success "Split services installed (app + relay + worker + media + bots)"
    else
        print_warning "Split-service setup failed — the single service is still installed and working"
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

# (Removed) setup_intel_dual_services — the Intel chat+image split (two venvs/services on
# 3051+3052) is obsolete; Intel now runs unified in one service via setup_single_service.

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
# Unified Intel Arc launcher: chat (llama.cpp SYCL) + native image gen (diffusers torch-XPU)
# from ONE venv (venv-unified: torch 2.12 XPU + llama-cpp-python SYCL + app deps). Replaces
# the old split (venv-ipex IPEX-LLM chat / venv-xpu-new image).
#
# Key: do NOT source a system /opt/intel/oneapi — torch 2.12 bundles its own oneAPI runtime in
# venv-unified/lib; mixing in a system oneAPI triggers the LIBUR_LOADER symbol mismatch.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# torch's bundled oneAPI runtime (venv-unified/lib) + system Level-Zero/IGC (lib64 or multiarch).
export LD_LIBRARY_PATH="$SCRIPT_DIR/venv-unified/lib:/usr/lib64:/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"

# MANDATORY for llama.cpp SYCL: without it SYCL silently selects the CPU device
# (symptom: ~2 tok/s instead of ~19). Harmless for torch-XPU image gen.
export ONEAPI_DEVICE_SELECTOR=level_zero:gpu
export ZES_ENABLE_SYSMAN=1
export SYCL_CACHE_PERSISTENT=1

cd "$SCRIPT_DIR"
exec "$SCRIPT_DIR/venv-unified/bin/python" run.py "$@"
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
# CPU / Nostr-only / Ollama wrapper script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# Local env overrides (DATABASE_URL, POSTERCHANAI_NOSTR_RELAY / _NOSTR_ONLY, …)
[ -f "$SCRIPT_DIR/data/secrets.env" ] && . "$SCRIPT_DIR/data/secrets.env"
exec "$SCRIPT_DIR/venv/bin/python" run.py "$@"
SCRIPT
}

create_service_file() {
    local VENV_PATH="$SCRIPT_DIR/venv"
    [ "$BACKEND" = "intel" ] && VENV_PATH="$SCRIPT_DIR/venv-unified"

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
Description=Posterchan AI (Intel Arc GPU, unified llama.cpp SYCL + image)
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

# (Removed) create_xpu_image_run_script / create_xpu_image_service_file — the separate
# port-3052 XPU image service is obsolete. Image gen now runs in the unified service
# (run-intel.sh → venv-unified) as a per-gen subprocess. See setup_image_deps in image.sh.

