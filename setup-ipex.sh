#!/bin/bash
# Setup script for IPEX-LLM backend with Intel Arc GPU support

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "Setting up Posterchanai with IPEX-LLM backend..."

# Check for Level Zero (required for Intel GPU compute)
if ! ldconfig -p | grep -q libze_loader; then
    echo ""
    echo "WARNING: Level Zero (libze_loader) not found!"
    echo "This is required for Intel GPU acceleration with IPEX-LLM."
    echo ""
    if [ -f /etc/gentoo-release ]; then
        echo "Install with: sudo emerge -av dev-libs/level-zero"
    elif [ -f /etc/arch-release ]; then
        echo "Install with: sudo pacman -S level-zero-loader"
    elif [ -f /etc/debian_version ]; then
        echo "Install with: sudo apt install level-zero"
    else
        echo "Please install the level-zero package for your distribution."
    fi
    echo ""
    read -p "Continue anyway? [y/N]: " CONTINUE
    if [[ ! "$CONTINUE" =~ ^[Yy] ]]; then
        exit 1
    fi
fi

# Check for patchelf (needed to fix executable stack on modern glibc)
if ! command -v patchelf &>/dev/null; then
    echo ""
    echo "WARNING: patchelf not found!"
    echo "This is needed to fix IPEX libraries on systems with glibc 2.41+."
    echo ""
    if [ -f /etc/gentoo-release ]; then
        echo "Install with: sudo emerge -av dev-util/patchelf"
    elif [ -f /etc/arch-release ]; then
        echo "Install with: sudo pacman -S patchelf"
    elif [ -f /etc/debian_version ]; then
        echo "Install with: sudo apt install patchelf"
    else
        echo "Please install patchelf for your distribution."
    fi
    echo ""
    read -p "Continue anyway? [y/N]: " CONTINUE
    if [[ ! "$CONTINUE" =~ ^[Yy] ]]; then
        exit 1
    fi
fi

# Check for Intel oneAPI — detect version and set ONEAPI_ROOT / ICX / ICPX
ONEAPI_FOUND=0
ONEAPI_ROOT=""
ICX=""
ICPX=""

for CANDIDATE_VARS in \
    /opt/intel/oneapi/2025.3/oneapi-vars.sh \
    /opt/intel/oneapi/2025.2/oneapi-vars.sh \
    /opt/intel/oneapi/2025.1/oneapi-vars.sh \
    /opt/intel/oneapi/2025.0/oneapi-vars.sh \
    /opt/intel/oneapi/2024.2/oneapi-vars.sh \
    /opt/intel/oneapi/setvars.sh \
    ~/intel/oneapi/setvars.sh; do
    if [ -f "$CANDIDATE_VARS" ]; then
        source "$CANDIDATE_VARS" --force 2>/dev/null || true
        ONEAPI_FOUND=1
        ONEAPI_ROOT="$(dirname "$CANDIDATE_VARS")"
        break
    fi
done

if [ "$ONEAPI_FOUND" -eq 0 ]; then
    echo "ERROR: Intel oneAPI not found!"
    echo "Please install Intel oneAPI Base Toolkit from:"
    echo "  https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit-download.html"
    exit 1
fi

# Resolve icx/icpx full paths (cmake needs them explicit for SYCL builds)
if command -v icx &>/dev/null && command -v icpx &>/dev/null; then
    ICX="$(command -v icx)"
    ICPX="$(command -v icpx)"
else
    # Fallback: search under known compiler dirs
    for CBIN in /opt/intel/oneapi/compiler/latest/bin /opt/intel/oneapi/compiler/2025.0/bin /opt/intel/oneapi/compiler/2024.2/bin; do
        if [ -x "$CBIN/icx" ] && [ -x "$CBIN/icpx" ]; then
            ICX="$CBIN/icx"
            ICPX="$CBIN/icpx"
            break
        fi
    done
fi

if [ -z "$ICX" ] || [ -z "$ICPX" ]; then
    echo "ERROR: icx/icpx not found after sourcing oneAPI environment."
    exit 1
fi

# Resolve MKL cmake dir
MKL_CMAKE_DIR=""
for D in /opt/intel/oneapi/mkl/latest/lib/cmake/mkl /opt/intel/oneapi/mkl/2025.0/lib/cmake/mkl /opt/intel/oneapi/mkl/2024.2/lib/cmake/mkl; do
    if [ -d "$D" ]; then MKL_CMAKE_DIR="$D"; break; fi
done

# Resolve IntelSYCL cmake dir
SYCL_CMAKE_DIR=""
for D in "$ONEAPI_ROOT/lib/cmake/IntelSYCL" /opt/intel/oneapi/2025.0/lib/cmake/IntelSYCL /opt/intel/oneapi/2024.2/lib/cmake/IntelSYCL; do
    if [ -d "$D" ]; then SYCL_CMAKE_DIR="$D"; break; fi
done

echo "Intel oneAPI found: $ONEAPI_ROOT"
echo "  icx:  $ICX"
echo "  icpx: $ICPX"

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

# Fix executable stack issue on modern glibc (2.41+)
# The IPEX library requires executable stack which is blocked by default
if command -v patchelf &>/dev/null; then
    echo ""
    echo "Fixing IPEX library executable stack flags..."
    IPEX_LIB=$(find venv-ipex/lib -name "libintel-ext-pt-cpu.so" 2>/dev/null | head -1)
    if [ -n "$IPEX_LIB" ]; then
        patchelf --clear-execstack "$IPEX_LIB" 2>/dev/null && echo "  Fixed: $IPEX_LIB" || echo "  Warning: Could not patch $IPEX_LIB"
    fi
    # Also fix any other libraries that might need it
    for lib in $(find venv-ipex/lib -name "*.so" -exec sh -c 'readelf -l "$1" 2>/dev/null | grep -q "RWE" && echo "$1"' _ {} \; 2>/dev/null); do
        patchelf --clear-execstack "$lib" 2>/dev/null && echo "  Fixed: $lib"
    done
fi

# Build llama-cpp-python with SYCL support for Intel Arc GPU
# We build from source because:
#   1. We need llama-cpp-python >= 0.3.22 for Qwen3.5 (qwen35 arch) support
#   2. oneAPI 2025.0 is missing work_group_static.hpp — we inject a stub
#   3. oneAPI 2025.0 is missing some newer GPU arch enums — we patch sycl_hw.cpp
#      (bmg_g31/ptl_h/ptl_u/wcl, not needed for Arc B580/B770 which are bmg_g21)
echo ""
echo "Building llama-cpp-python 0.3.22 with Intel SYCL support..."
echo "This may take 5-10 minutes..."

# Install build tools required by scikit-build-core
pip install --quiet scikit-build-core cmake ninja

# Download source
LLAMA_VERSION="0.3.22"
LLAMA_TMP="$(mktemp -d)"
LLAMA_SRC_DIR="$LLAMA_TMP/llama_cpp_python-${LLAMA_VERSION}"
pip download "llama-cpp-python==${LLAMA_VERSION}" --no-deps -d "$LLAMA_TMP" --quiet
tar xzf "$LLAMA_TMP/llama_cpp_python-${LLAMA_VERSION}.tar.gz" -C "$LLAMA_TMP"

# Patch sycl_hw.cpp: remove GPU arch entries not in oneAPI 2025.0 headers
# (bmg_g31=future Battlemage, ptl_h/ptl_u=Panther Lake, wcl=Wildcat Lake)
# Arc B580/B770 are bmg_g21 which IS present — unknown archs fall back gracefully
SYCL_HW="$LLAMA_SRC_DIR/vendor/llama.cpp/ggml/src/ggml-sycl/sycl_hw.cpp"
if [ -f "$SYCL_HW" ]; then
    python3 - "$SYCL_HW" <<'PYEOF'
import re, sys
path = sys.argv[1]
text = open(path).read()
remove = [
    r'[ \t]*\{gpu_arch::intel_gpu_bmg_g31,[^\n]*\},?\n',
    r'[ \t]*\{gpu_arch::intel_gpu_ptl_h,[^\n]*\},?\n',
    r'[ \t]*\{gpu_arch::intel_gpu_ptl_u,[^\n]*\},?\n',
    r'[ \t]*\{gpu_arch::intel_gpu_wcl,[^\n]*\}\n',
]
for pat in remove:
    text = re.sub(pat, '', text)
# Fix trailing comma on last map entry if needed
text = re.sub(r',\s*\n(\s*\})', r'\n\1', text)
open(path, 'w').write(text)
print(f"  Patched sycl_hw.cpp: removed future GPU arch entries")
PYEOF
fi

# Create work_group_static.hpp stub (missing from oneAPI 2025.0/2024.2 headers)
# This header was added in a later oneAPI release; the stub is the upstream content
# from intel/llvm and is needed to compile the SYCL flash attention tile kernel.
STUB_DIR="$(mktemp -d)"
mkdir -p "$STUB_DIR/sycl/ext/oneapi/experimental"
cat > "$STUB_DIR/sycl/ext/oneapi/experimental/work_group_static.hpp" <<'HEOF'
#pragma once
#include <sycl/detail/defines_elementary.hpp>
#include <sycl/exception.hpp>
#include <type_traits>
namespace sycl { inline namespace _V1 { namespace ext::oneapi { namespace experimental {
#ifdef __SYCL_DEVICE_ONLY__
#define __SYCL_WG_SCOPE [[__sycl_detail__::wg_scope]]
#else
#define __SYCL_WG_SCOPE
#endif
template <typename T> class __SYCL_WG_SCOPE work_group_static final {
public:
  static_assert(std::is_trivially_destructible_v<T> && std::is_trivially_constructible_v<T>,
      "Can only be used with trivially constructible and destructible types");
  static_assert(!std::is_const_v<T> && !std::is_volatile_v<T>,
      "Can only be used with non const and non volatile types");
  __SYCL_ALWAYS_INLINE work_group_static() = default;
  work_group_static(const work_group_static &) = delete;
  work_group_static &operator=(const work_group_static &) = delete;
  operator T &() noexcept { return data; }
  template <class TArg = T, typename = std::enable_if_t<!std::is_array_v<TArg>>>
  work_group_static &operator=(const T &value) noexcept { data = value; return *this; }
  T *operator&() noexcept { return &data; }
private:
  T data;
};
#undef __SYCL_WG_SCOPE
} } } }
HEOF
echo "  Created work_group_static.hpp stub"

# Build with SYCL, injecting stub header via CPLUS_INCLUDE_PATH
# CMAKE_PREFIX_PATH and MKL_DIR/IntelSYCL_DIR let cmake find oneAPI libraries
CMAKE_PREFIX="${ONEAPI_ROOT}/lib/cmake"
CMAKE_ARGS_VAL="-DGGML_SYCL=ON \
  -DCMAKE_C_COMPILER=${ICX} \
  -DCMAKE_CXX_COMPILER=${ICPX} \
  -DCMAKE_PREFIX_PATH=${CMAKE_PREFIX} \
  -DCMAKE_CXX_FLAGS=-I${STUB_DIR}"

if [ -n "$MKL_CMAKE_DIR" ]; then
    CMAKE_ARGS_VAL="$CMAKE_ARGS_VAL -DMKL_DIR=${MKL_CMAKE_DIR}"
fi
if [ -n "$SYCL_CMAKE_DIR" ]; then
    CMAKE_ARGS_VAL="$CMAKE_ARGS_VAL -DIntelSYCL_DIR=${SYCL_CMAKE_DIR}"
fi

CPLUS_INCLUDE_PATH="$STUB_DIR" \
CMAKE_ARGS="$CMAKE_ARGS_VAL" \
pip install "$LLAMA_SRC_DIR" \
    --force-reinstall \
    --no-cache-dir \
    --no-build-isolation

# Clean up temp dirs
rm -rf "$LLAMA_TMP" "$STUB_DIR"

# Pin numpy<2 — ipex-llm/bigdl requires numpy 1.x; llama-cpp-python pulled in 2.x
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
echo "If you see 'libze_loader.so.1: cannot open shared object' error:"
echo "  Install Level Zero: sudo emerge -av dev-libs/level-zero (Gentoo)"
echo "                      sudo apt install level-zero (Debian/Ubuntu)"
echo ""
echo "If you see 'cannot enable executable stack' error:"
echo "  The installer should have fixed this automatically with patchelf."
echo "  If it persists, manually run:"
echo "    patchelf --clear-execstack venv-ipex/lib/python*/site-packages/intel_extension_for_pytorch/lib/libintel-ext-pt-cpu.so"
echo ""
echo "If you see 'libmkl_sycl_blas.so.4: cannot open' error:"
echo "  You may have multiple oneAPI versions. Ensure oneAPI 2024.2 is installed"
echo "  or the run script includes both library paths."
echo ""
echo "If you see 'NumPy 1.x cannot run in NumPy 2.x' error:"
echo "  Run: venv-ipex/bin/pip install 'numpy<2'"
echo ""
echo "If IPEX-LLM falls back to CPU (standard llama-cpp-python):"
echo "  1. Check that Intel oneAPI is properly installed"
echo "  2. Ensure dev-libs/level-zero is installed"
echo "  3. Check journalctl -u posterchanai-ipex for specific errors"
echo ""
echo "Thinking-mode models (Qwen3-abliterated, DeepSeek-R1):"
echo "  The system detects these models and attempts to suppress thinking mode."
echo "  Success rate: ~50% with abliterated models (they're designed to ignore instructions)"
echo "  For best results:"
echo "    - Use standard Qwen2.5, Llama-3, or Mistral (100% reliable)"
echo "    - Or use non-abliterated Qwen3 which follows instructions better"
echo "    - Abliterated models work but may occasionally show thinking process"
