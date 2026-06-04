#!/bin/bash
# Detection Functions - GPU and distro detection
# Sourced by install.sh

# Global variables set by detection
DISTRO=""
GPU_TYPE="cpu"
GPU_NAME="CPU Only"

detect_distro() {
    # Prefer /etc/os-release (works for derivatives via ID + ID_LIKE), fall back to release files.
    DISTRO="unknown"
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        local ids="$ID $ID_LIKE"
        case " $ids " in
            *gentoo*)                         DISTRO="gentoo" ;;
            *arch*|*manjaro*)                 DISTRO="arch" ;;
            *suse*|*opensuse*)                DISTRO="suse" ;;   # openSUSE Leap/Tumbleweed, SLES
            *fedora*|*rhel*|*centos*)         DISTRO="fedora" ;; # dnf/rpm family
            *debian*|*ubuntu*)                DISTRO="debian" ;;
        esac
    fi
    if [ "$DISTRO" = "unknown" ]; then
        if   [ -f /etc/gentoo-release ];  then DISTRO="gentoo"
        elif [ -f /etc/arch-release ];    then DISTRO="arch"
        elif [ -f /etc/SUSE-brand ] || [ -f /etc/SuSE-release ]; then DISTRO="suse"
        elif [ -f /etc/fedora-release ]; then DISTRO="fedora"
        elif [ -f /etc/debian_version ]; then DISTRO="debian"
        fi
    fi
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

# Detect AMD GPU architecture version (for HSA_OVERRIDE_GFX_VERSION)
detect_amd_gfx_version() {
    local GFX_VERSION=""
    if command -v rocminfo &>/dev/null; then
        GFX_VERSION=$(rocminfo 2>/dev/null | grep -o 'gfx[0-9]*' | head -1)
        if [ -n "$GFX_VERSION" ]; then
            print_success "Detected AMD GPU: $GFX_VERSION"
        fi
    fi
    echo "$GFX_VERSION"
}

# Detect Intel oneAPI installation path
detect_oneapi_path() {
    local ONEAPI_PATH=""
    if [ -f /opt/intel/oneapi/2025.0/oneapi-vars.sh ]; then
        ONEAPI_PATH="/opt/intel/oneapi/2025.0/oneapi-vars.sh"
    elif [ -f /opt/intel/oneapi/2024.2/oneapi-vars.sh ]; then
        ONEAPI_PATH="/opt/intel/oneapi/2024.2/oneapi-vars.sh"
    elif [ -f /opt/intel/oneapi/setvars.sh ]; then
        ONEAPI_PATH="/opt/intel/oneapi/setvars.sh"
    elif [ -f ~/intel/oneapi/setvars.sh ]; then
        ONEAPI_PATH="$HOME/intel/oneapi/setvars.sh"
    fi
    echo "$ONEAPI_PATH"
}

# Detect Intel oneAPI root directory
detect_oneapi_root() {
    if [ -d /opt/intel/oneapi/2025.0 ]; then
        echo "/opt/intel/oneapi/2025.0"
    elif [ -d /opt/intel/oneapi/2024.2 ]; then
        echo "/opt/intel/oneapi/2024.2"
    elif [ -d /opt/intel/oneapi ]; then
        echo "/opt/intel/oneapi"
    fi
}
