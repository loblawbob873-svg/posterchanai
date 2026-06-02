#!/bin/bash
# Dependency Checking and Installation Instructions
# Sourced by install.sh

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

    # Check optional dependencies
    if ! command -v ffmpeg &>/dev/null; then
        print_warning "ffmpeg not found - music transcoding and video compression will be unavailable"
        echo "  Install ffmpeg for music streaming and the 'compress' command (video)"
    else
        print_success "ffmpeg found (music transcoding + video compression available)"
    fi

    # Firefox - needed for the 'screenshot' command (uses Firefox's native --screenshot)
    if ! command -v firefox &>/dev/null && ! command -v firefox-bin &>/dev/null; then
        print_warning "firefox not found - the 'screenshot' command will be unavailable"
        case "$DISTRO" in
            gentoo) echo "  Install with: emerge -av www-client/firefox-bin" ;;
            arch)   echo "  Install with: pacman -S firefox" ;;
            debian) echo "  Install with: apt install firefox-esr" ;;
            fedora) echo "  Install with: dnf install firefox" ;;
            *)      echo "  Install firefox for your distribution" ;;
        esac
    else
        print_success "firefox found (screenshot command available)"
    fi

    # Check for pax-utils (scanelf) - needed for Intel Arc on hardened kernels
    if [ "$BACKEND" = "intel" ]; then
        if ! command -v scanelf &>/dev/null; then
            print_warning "scanelf (pax-utils) not found"
            echo "  Required to fix IPEX library permissions on hardened kernels (Gentoo)"
            case "$DISTRO" in
                gentoo) echo "  Install with: emerge -av app-misc/pax-utils" ;;
                arch) echo "  Install with: pacman -S pax-utils" ;;
                debian) echo "  Install with: apt install pax-utils" ;;
                *) echo "  Install pax-utils for your distribution" ;;
            esac
        else
            print_success "scanelf found (pax-utils)"
        fi
    fi
}

show_install_instructions() {
    detect_distro

    echo -e "${YELLOW}Please install the required packages:${NC}"
    echo ""

    case "$DISTRO" in
        gentoo)
            show_gentoo_instructions
            ;;
        arch)
            show_arch_instructions
            ;;
        debian)
            show_debian_instructions
            ;;
        fedora)
            show_fedora_instructions
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

show_gentoo_instructions() {
    echo -e "${BOLD}Gentoo Linux:${NC}"
    echo ""
    echo "  # Base dependencies"
    echo "  emerge -av dev-lang/python dev-python/pip dev-build/cmake sys-devel/gcc"
    echo ""
    echo "  # For AMD GPU (ROCm):"
    echo "  echo -e 'dev-build/rocm-cmake\ndev-util/hipcc\ndev-libs/rocm-core\ndev-libs/roct-thunk-interface\ndev-libs/rocm-device-libs\ndev-libs/rocr-runtime\ndev-libs/rocm-comgr\ndev-util/rocminfo\ndev-util/rocm-smi\ndev-libs/rocm-opencl-runtime\ndev-util/hip\nsci-libs/hipBLAS\nsci-libs/hipBLAS-common\nsci-libs/rocBLAS\nsci-libs/rocSOLVER\ndev-util/Tensile' | sudo tee /etc/portage/package.accept_keywords/rocm"
    echo "  emerge -av dev-libs/rocm-opencl-runtime dev-util/hip dev-libs/rocr-runtime sci-libs/hipBLAS"
    echo ""
    echo "  # For Intel Arc GPU:"
    echo "  echo 'dev-util/intel-graphics-compiler no-distcc.conf' | sudo tee -a /etc/portage/package.env/intel-graphics-compiler"
    echo "  emerge -av dev-libs/intel-compute-runtime dev-libs/level-zero app-misc/pax-utils"
    echo "  # pax-utils provides scanelf to fix IPEX library permissions on hardened kernels"
    echo ""
    echo "  # For NVIDIA GPU:"
    echo "  emerge -av x11-drivers/nvidia-drivers dev-util/nvidia-cuda-toolkit"
}

show_arch_instructions() {
    echo -e "${BOLD}Arch Linux:${NC}"
    echo "  pacman -S python python-pip cmake gcc git"
    echo ""
    echo "  # For AMD GPU (ROCm):"
    echo "  pacman -S rocm-hip-sdk rocm-opencl-sdk"
    echo ""
    echo "  # For Intel Arc GPU: Install intel-oneapi-basekit from AUR"
    echo "  # For NVIDIA GPU: pacman -S nvidia cuda"
}

show_debian_instructions() {
    echo -e "${BOLD}Debian/Ubuntu:${NC}"
    echo "  apt install python3 python3-pip python3-venv cmake build-essential git"
    echo ""
    echo "  # For AMD GPU (ROCm):"
    echo "  wget https://repo.radeon.com/amdgpu-install/latest/ubuntu/\$(lsb_release -cs)/amdgpu-install_6.0.60002-1_all.deb"
    echo "  apt install ./amdgpu-install_*.deb"
    echo "  amdgpu-install --usecase=rocm"
    echo ""
    echo "  # For NVIDIA GPU: apt install nvidia-driver nvidia-cuda-toolkit"
}

show_fedora_instructions() {
    echo -e "${BOLD}Fedora:${NC}"
    echo "  dnf install python3 python3-pip cmake gcc-c++ git"
    echo ""
    echo "  # For AMD GPU (ROCm):"
    echo "  dnf install https://repo.radeon.com/amdgpu-install/latest/rhel/\$(rpm -E %rhel)/amdgpu-install-*.noarch.rpm"
    echo "  amdgpu-install --usecase=rocm"
    echo ""
    echo "  # For NVIDIA GPU: dnf install nvidia-driver cuda"
}
