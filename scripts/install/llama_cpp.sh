#!/bin/bash
# llama-cpp-python Installation
# Sourced by install.sh

setup_llama_cpp() {
    print_step "Installing llama-cpp-python..."

    # Activate the appropriate venv
    local VENV_NAME="${CHAT_VENV_NAME:-venv}"
    source "$SCRIPT_DIR/$VENV_NAME/bin/activate"

    case "$BACKEND" in
        intel)
            setup_llama_cpp_intel
            ;;
        nvidia)
            setup_llama_cpp_nvidia
            ;;
        amd)
            setup_llama_cpp_amd
            ;;
        cpu)
            setup_llama_cpp_cpu
            ;;
        ollama)
            deactivate
            print_success "Using Ollama backend (no llama-cpp-python needed)"
            return
            ;;
    esac

    deactivate
    print_success "llama-cpp-python installed"
}

setup_llama_cpp_intel() {
    # Gentoo-specific: Configure package.env for intel-graphics-compiler
    if [ "$DISTRO" = "gentoo" ]; then
        local IGC_ENV_FILE="/etc/portage/package.env/intel-graphics-compiler"
        if [ ! -f "$IGC_ENV_FILE" ] || ! grep -q "no-distcc.conf" "$IGC_ENV_FILE" 2>/dev/null; then
            print_step "Configuring Gentoo package.env for Intel graphics compiler..."
            sudo mkdir -p /etc/portage/package.env
            echo 'dev-util/intel-graphics-compiler no-distcc.conf' | sudo tee -a "$IGC_ENV_FILE" > /dev/null
            print_success "Added no-distcc.conf for intel-graphics-compiler"
        fi
    fi

    # Check for Level Zero
    if ! ldconfig -p | grep -q libze_loader; then
        print_warning "Level Zero (libze_loader) not found!"
        echo ""
        echo "  This is required for Intel GPU acceleration with IPEX-LLM."
        case "$DISTRO" in
            gentoo) echo "  Install with: sudo emerge -av dev-libs/level-zero" ;;
            arch) echo "  Install with: sudo pacman -S level-zero-loader" ;;
            debian) echo "  Install with: sudo apt install level-zero" ;;
            *) echo "  Please install the level-zero package for your distribution." ;;
        esac
        echo ""
        read -p "  Continue anyway? [y/N]: " CONTINUE_LZ
        if [[ ! "$CONTINUE_LZ" =~ ^[Yy] ]]; then
            exit 1
        fi
    fi

    # Check for patchelf
    if ! command -v patchelf &>/dev/null; then
        print_warning "patchelf not found!"
        echo ""
        echo "  This is needed to fix IPEX libraries on systems with glibc 2.41+."
        case "$DISTRO" in
            gentoo) echo "  Install with: sudo emerge -av dev-util/patchelf" ;;
            arch) echo "  Install with: sudo pacman -S patchelf" ;;
            debian) echo "  Install with: sudo apt install patchelf" ;;
            *) echo "  Please install patchelf for your distribution." ;;
        esac
        echo ""
        read -p "  Continue anyway? [y/N]: " CONTINUE_PF
        if [[ ! "$CONTINUE_PF" =~ ^[Yy] ]]; then
            exit 1
        fi
    fi

    # Source Intel oneAPI
    local ONEAPI_PATH
    ONEAPI_PATH=$(detect_oneapi_path)
    if [ -z "$ONEAPI_PATH" ]; then
        print_error "Intel oneAPI not found!"
        echo ""
        echo "  Please install Intel oneAPI Base Toolkit:"
        echo "  https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit-download.html"
        exit 1
    fi

    source "$ONEAPI_PATH" --force >/dev/null 2>&1
    print_success "Intel oneAPI loaded from $ONEAPI_PATH"

    # Get oneAPI root directory and set CMAKE_PREFIX_PATH for the build
    # This is required for CMake to find MKL, DNNL, and IntelSYCL
    local ONEAPI_ROOT
    ONEAPI_ROOT=$(detect_oneapi_root)
    export CMAKE_PREFIX_PATH="$ONEAPI_ROOT/lib/cmake:$ONEAPI_ROOT:${CMAKE_PREFIX_PATH:-}"
    export MKLROOT="$ONEAPI_ROOT"
    export DNNLROOT="$ONEAPI_ROOT"

    # Create libittnotify stub if not exists
    create_vtune_stub

    # Install IPEX-LLM with cpp support (includes torch 2.2.0 and intel-extension-for-pytorch)
    # This provides GPU-accelerated GGML inference for Intel Arc GPUs
    echo "  Installing IPEX-LLM with cpp support..."
    echo "  This includes PyTorch 2.2.0 and Intel Extension for PyTorch..."
    pip install --pre --upgrade "ipex-llm[cpp]" -q

    # Install compatible torchvision
    echo "  Installing compatible torchvision..."
    pip install torchvision==0.17.0 -q 2>/dev/null || pip install torchvision -q

    # Verify IPEX installation
    if python -c "import intel_extension_for_pytorch" 2>/dev/null; then
        print_success "Intel Extension for PyTorch installed successfully"
    else
        print_warning "Intel Extension for PyTorch import failed - GPU may not be used"
    fi

    # Verify IPEX-LLM GGML
    if python -c "from ipex_llm.ggml.model.llama import Llama" 2>/dev/null; then
        print_success "IPEX-LLM GGML backend installed successfully"
    else
        print_warning "IPEX-LLM GGML not available - will fall back to llama-cpp-python"
    fi

    # Fix executable stack issue on modern glibc (2.41+)
    # This is required for systems with strict memory protection
    if command -v patchelf &>/dev/null; then
        echo "  Fixing IPEX library executable stack flags..."
        local IPEX_LIB
        # Find the library in the current venv
        IPEX_LIB=$(python -c "import intel_extension_for_pytorch, os; print(os.path.dirname(intel_extension_for_pytorch.__file__) + '/lib/libintel-ext-pt-cpu.so')" 2>/dev/null)
        if [ -f "$IPEX_LIB" ]; then
            patchelf --clear-execstack "$IPEX_LIB" 2>/dev/null && print_success "Fixed executable stack on IPEX library" || true
        else
            # Fallback: search in venv
            IPEX_LIB=$(find "$VIRTUAL_ENV/lib" -name "libintel-ext-pt-cpu.so" 2>/dev/null | head -1)
            if [ -n "$IPEX_LIB" ]; then
                patchelf --clear-execstack "$IPEX_LIB" 2>/dev/null && print_success "Fixed executable stack on IPEX library" || true
            fi
        fi
    fi

    # Install llama-cpp-python with SYCL support for Intel Arc GPU
    # This is the primary backend for GGUF models on Intel Arc
    # IMPORTANT: Must use icx/icpx compilers from oneAPI for SYCL support
    # Use full paths to compilers to ensure they're found during pip build
    echo "  Building llama-cpp-python with SYCL backend for Intel Arc..."
    echo "  Using Intel compilers (icx/icpx) for SYCL support..."
    echo "  This compiles with GPU support - may take 5-10 minutes..."
    CMAKE_ARGS="-DGGML_SYCL=ON -DCMAKE_C_COMPILER=$ONEAPI_ROOT/bin/icx -DCMAKE_CXX_COMPILER=$ONEAPI_ROOT/bin/icpx" \
        pip install llama-cpp-python --force-reinstall --no-cache-dir -q || {
        print_warning "SYCL build failed, installing pre-built (may be CPU-only)..."
        pip install llama-cpp-python -q
    }

    # Verify SYCL support (need LD_LIBRARY_PATH for runtime libs like libsvml.so)
    export LD_LIBRARY_PATH="$ONEAPI_ROOT/lib:${LD_LIBRARY_PATH:-}"
    if python -c "import llama_cpp; print('GPU offload:', llama_cpp.llama_supports_gpu_offload())" 2>/dev/null | grep -q "True"; then
        print_success "llama-cpp-python installed with GPU (SYCL) support"
    else
        print_warning "llama-cpp-python may be CPU-only - check SYCL compilation"
    fi
}

setup_llama_cpp_nvidia() {
    echo "  Building with CUDA backend..."

    # Check GCC version - CUDA has strict requirements
    local GCC_MAJOR
    GCC_MAJOR=$(gcc -dumpversion | cut -d. -f1)
    local CUDA_HOST_COMPILER=""

    if [ "$GCC_MAJOR" -gt 14 ]; then
        print_warning "GCC $GCC_MAJOR detected, but CUDA requires GCC 14 or earlier"

        # Look for compatible GCC versions
        for ver in 14 13 12 11; do
            if [ -x "/usr/bin/gcc-$ver" ] && [ -x "/usr/bin/g++-$ver" ]; then
                CUDA_HOST_COMPILER="/usr/bin/gcc-$ver"
                print_success "Found compatible compiler: gcc-$ver"
                break
            fi
        done

        if [ -z "$CUDA_HOST_COMPILER" ]; then
            print_error "No compatible GCC found (need GCC 14 or earlier)"
            echo ""
            echo "  Please install GCC 14:"
            case "$DISTRO" in
                gentoo) echo "    emerge -av sys-devel/gcc:14" ;;
                arch) echo "    pacman -S gcc13" ;;
                debian) echo "    apt install gcc-14 g++-14" ;;
                fedora) echo "    dnf install gcc g++" ;;
                *) echo "    Install GCC 14 or earlier" ;;
            esac
            exit 1
        fi

        export CC="$CUDA_HOST_COMPILER"
        export CXX="${CUDA_HOST_COMPILER/gcc/g++}"
        export CUDAHOSTCXX="$CUDA_HOST_COMPILER"
        export CMAKE_ARGS="-DGGML_CUDA=ON -DCMAKE_CUDA_HOST_COMPILER=$CUDA_HOST_COMPILER"
    else
        export CMAKE_ARGS="-DGGML_CUDA=ON"
    fi

    echo "  This may take 5-10 minutes..."
    pip install llama-cpp-python --force-reinstall --no-cache-dir -q
}

setup_llama_cpp_amd() {
    # Check for ROCm
    if ! command -v rocminfo &>/dev/null && [ ! -d /opt/rocm ]; then
        print_error "ROCm not found!"
        echo ""
        echo "  Please install ROCm first. See install instructions for your distro."
        exit 1
    fi

    # Gentoo-specific warning
    if [ "$DISTRO" = "gentoo" ]; then
        echo ""
        print_warning "Gentoo ROCm Note:"
        echo "  llama-cpp-python may not build with HIP support on Gentoo"
        echo "  due to non-standard library paths. Will fall back to CPU if needed."
        echo ""
    fi

    echo "  Building with ROCm/HIP backend (may fall back to CPU)..."
    export CMAKE_ARGS="-DGGML_HIP=ON"

    # Set HIP path
    if [ -d /opt/rocm ]; then
        export HIP_PATH=/opt/rocm
        export ROCM_PATH=/opt/rocm
    else
        export HIP_PATH=/usr
        export ROCM_PATH=/usr
        export CMAKE_PREFIX_PATH="/usr/lib64/cmake:${CMAKE_PREFIX_PATH:-}"
    fi

    pip install llama-cpp-python --force-reinstall --no-cache-dir -q || {
        print_warning "HIP build failed, falling back to CPU-only..."
        unset CMAKE_ARGS
        pip install llama-cpp-python --force-reinstall --no-cache-dir -q
    }
}

setup_llama_cpp_cpu() {
    echo "  Building CPU-only version..."
    # Disable GGML_NATIVE to avoid Intel SVML dependency
    export CMAKE_ARGS="-DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=ON"
    pip install llama-cpp-python --force-reinstall --no-cache-dir -q
}

create_vtune_stub() {
    if [ -f /usr/local/lib/libittnotify.so ]; then
        return
    fi

    print_step "Creating VTune stub library..."
    local STUB_DIR
    STUB_DIR=$(mktemp -d)

    cat > "$STUB_DIR/ittnotify_stub.c" << 'STUBCODE'
// Stub for Intel VTune symbols
void __itt_pause(void) {}
void __itt_resume(void) {}
int __itt_api_init(void) { return 0; }
void* __itt_null = 0;
int iJIT_NotifyEvent(int, void*) { return 0; }
STUBCODE

    gcc -shared -fPIC -o "$STUB_DIR/libittnotify.so" "$STUB_DIR/ittnotify_stub.c" 2>/dev/null || true
    if [ -f "$STUB_DIR/libittnotify.so" ]; then
        sudo cp "$STUB_DIR/libittnotify.so" /usr/local/lib/
        sudo ldconfig
        print_success "VTune stub library installed"
    fi
    rm -rf "$STUB_DIR"
}
