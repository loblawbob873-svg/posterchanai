# IPEX-LLM Setup Guide

IPEX-LLM provides optimized LLM inference for Intel Arc GPUs. This guide covers installation on Gentoo, Debian/Ubuntu, and Fedora.

## Prerequisites

- Intel Arc GPU (A770, A750, A380, etc.)
- Python 3.11 (required - IPEX-LLM is not compatible with Python 3.12+)
- **Intel oneAPI Base Toolkit 2025.0+** (required for llama-cpp-python SYCL backend with Qwen3 support)

## Version Requirements

These are the **actual versions running on the A770 box (server1)** as of 2026-06-04.

| Component | Version | Notes |
|-----------|---------|-------|
| Intel oneAPI Base Toolkit | **2025.0** (pin — do NOT use 2025.1+) | 2025.1+ breaks `ipex 2.5.10`; only needed for llama-cpp ≥0.3.23 which we can't use anyway |
| Python | 3.11.x | 3.12+ not supported |
| llama-cpp-python | **0.3.22** | Built with SYCL. `>=0.3.23` needs oneAPI 2025.1+ (`work_group_static.hpp`) → won't build here |
| torch | **2.5.1+cxx11.abi** | Do NOT upgrade without testing |
| intel-extension-for-pytorch | **2.5.10+xpu** | Built for oneAPI 2025.0 |
| ipex-llm | **2.3.0b20251110** | Optional, for HuggingFace models |

### OS-level Intel GPU runtime (system libs, not pip)
This layer is **separate from Python** and is shared by BOTH GPU services (LLM + image). The
**Intel Graphics Compiler (IGC) version is the single most important knob** on the A770:

| Package | Version | Notes |
|---------|---------|-------|
| Intel Graphics Compiler (IGC) | **2.35.5** | ⭐ the fix. Unblocks LLM long-context/14B (`__spirv_GroupBroadcast`) AND image gen at ≥768 (oneDNN conv). See below. |
| dev-libs/intel-compute-runtime | **25.40.35563.4** | Keep on 25.40 — **26.x will not build** (gmmlib→IGC `__spirv_ocl_vloadn` skew) |
| dev-libs/level-zero | **1.28.6** | |
| media-libs/gmmlib | **22.10.0** | |

**IGC 2.35.5 install:** Gentoo/most distros only ship up to 2.35.2 (which is *not enough* — it
lacks `__spirv_GroupBroadcast` and breaks image gen ≥768). Install the upstream 2.35.5 libs
system-wide with the bundled, distro-agnostic helper:

```bash
sudo ./scripts/install-igc.sh            # uses staged /opt/igc-2.35.5 if present, else downloads
sudo ./scripts/install-igc.sh --download # force fetch the v2.35.5 debs from GitHub
```

It installs **four** libs — `libigc`, `libigdfcl`, `libiga64`, **and `libopencl-clang2.so.17`**
(the last is easy to miss; without it image gen later dies with `CL_OUT_OF_HOST_MEMORY` at the
text encoder) — backing up any existing IGC to `/opt/igc-backup-*`.

> History: earlier we believed image gen (needed IGC 2.23.0) and the LLM 14B (needed IGC 2.35.x)
> were **mutually exclusive** on the shared system IGC. That is **resolved** — with the image
> service on the modern **torch 2.8 XPU** stack (see [Image generation](#image-generation-xpu)),
> IGC **2.35.5** serves both. Do not downgrade IGC to 2.23.0 anymore.

Pin the `emerge`'d runtime packages as `~amd64` in `/etc/portage/package.accept_keywords` (or
`@world` will downgrade them); IGC itself is installed by the script above, outside portage.
Arc DB settings: `llm_flash_attn=false` (flash-attn is broken on Arc SYCL even with new IGC), `llm_n_batch=1024`.

---

## Gentoo Linux

### 1. Install Intel oneAPI Base Toolkit 2025.0

```bash
cd /tmp
wget https://registrationcenter-download.intel.com/akdlm/IRC_NAS/96aa5993-5b22-4a9b-91ab-da679f422594/intel-oneapi-base-toolkit-2025.0.0.885_offline.sh
chmod +x intel-oneapi-base-toolkit-2025.0.0.885_offline.sh
sudo bash ./intel-oneapi-base-toolkit-2025.0.0.885_offline.sh -a --silent --eula accept
```

### 2. Install Python 3.11

```bash
sudo emerge -av dev-lang/python:3.11
```

### 3. Install required tools

```bash
sudo emerge -av sys-apps/util-linux app-misc/pax-utils
```

### 4. Create IPEX virtual environment

```bash
cd /path/to/posterchanai
python3.11 -m venv venv-ipex
source venv-ipex/bin/activate

# Source oneAPI environment
source /opt/intel/oneapi/2025.0/oneapi-vars.sh

# Install llama-cpp-python with SYCL support
CMAKE_ARGS="-DGGML_SYCL=on -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx" \
    pip install llama-cpp-python==0.3.22 --no-cache-dir

# Install app dependencies
pip install -r requirements.txt
pip install pytz websockets uvicorn[standard]

# Optional: Install IPEX-LLM for HuggingFace models
# (versions below are what's actually running on server1; verify the index URL has them)
pip install torch==2.5.1+cxx11.abi \
    intel-extension-for-pytorch==2.5.10+xpu \
    --extra-index-url https://pytorch-extension.intel.com/release-whl/stable/xpu/us/
pip install ipex-llm[xpu]==2.3.0b20251110 transformers>=4.37.0
```

### 5. Create VTune stub library

Intel's PyTorch expects VTune profiler symbols. Create a stub:

```bash
cat > /tmp/ittnotify_stub.c << 'EOF'
void __itt_pause(void) {}
void __itt_resume(void) {}
int iJIT_NotifyEvent(int event_type, void *event_data) { return 0; }
void* iJIT_GetNewMethodID(void) { return (void*)0; }
int iJIT_IsProfilingActive(void) { return 0; }
void __itt_thread_set_name(const char* name) {}
void __itt_frame_begin_v3(void* domain, void* id) {}
void __itt_frame_end_v3(void* domain, void* id) {}
void* __itt_domain_create(const char* name) { return (void*)0; }
void* __itt_string_handle_create(const char* name) { return (void*)0; }
void __itt_task_begin(void* domain, void* taskid, void* parentid, void* name) {}
void __itt_task_end(void* domain) {}
EOF

gcc -shared -fPIC -o /tmp/libittnotify.so /tmp/ittnotify_stub.c
sudo cp /tmp/libittnotify.so /usr/local/lib/
sudo ldconfig
```

### 6. Install systemd service

```bash
# Copy and edit the example service file
cp posterchanai-ipex.service.example posterchanai-ipex.service

# Edit paths in the service file
sed -i 's|YOUR_USERNAME|'$(whoami)'|g' posterchanai-ipex.service
sed -i 's|/path/to/posterchanai|'$(pwd)'|g' posterchanai-ipex.service

# Install and enable
sudo cp posterchanai-ipex.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable posterchanai-ipex
sudo systemctl start posterchanai-ipex
```

---

## Debian / Ubuntu

### 1. Install Intel oneAPI Base Toolkit 2025.0

```bash
cd /tmp
wget https://registrationcenter-download.intel.com/akdlm/IRC_NAS/96aa5993-5b22-4a9b-91ab-da679f422594/intel-oneapi-base-toolkit-2025.0.0.885_offline.sh
chmod +x intel-oneapi-base-toolkit-2025.0.0.885_offline.sh
sudo bash ./intel-oneapi-base-toolkit-2025.0.0.885_offline.sh -a --silent --eula accept
```

### 2. Install Python 3.11 and dependencies

```bash
sudo apt install python3.11 python3.11-venv python3.11-dev gcc g++
```

### 3. Create IPEX virtual environment

```bash
cd /path/to/posterchanai
python3.11 -m venv venv-ipex
source venv-ipex/bin/activate

# Source oneAPI environment
source /opt/intel/oneapi/2025.0/oneapi-vars.sh

# Install llama-cpp-python with SYCL support
CMAKE_ARGS="-DGGML_SYCL=on -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx" \
    pip install llama-cpp-python==0.3.22 --no-cache-dir

# Install app dependencies
pip install -r requirements.txt
pip install pytz websockets uvicorn[standard]
```

### 4. Create VTune stub library

```bash
cat > /tmp/ittnotify_stub.c << 'EOF'
void __itt_pause(void) {}
void __itt_resume(void) {}
int iJIT_NotifyEvent(int event_type, void *event_data) { return 0; }
void* iJIT_GetNewMethodID(void) { return (void*)0; }
int iJIT_IsProfilingActive(void) { return 0; }
void __itt_thread_set_name(const char* name) {}
void __itt_frame_begin_v3(void* domain, void* id) {}
void __itt_frame_end_v3(void* domain, void* id) {}
void* __itt_domain_create(const char* name) { return (void*)0; }
void* __itt_string_handle_create(const char* name) { return (void*)0; }
void __itt_task_begin(void* domain, void* taskid, void* parentid, void* name) {}
void __itt_task_end(void* domain) {}
EOF

gcc -shared -fPIC -o /tmp/libittnotify.so /tmp/ittnotify_stub.c
sudo cp /tmp/libittnotify.so /usr/local/lib/
sudo ldconfig
```

### 5. Install systemd service

```bash
cp posterchanai-ipex.service.example posterchanai-ipex.service

# Edit paths
sed -i 's|YOUR_USERNAME|'$(whoami)'|g' posterchanai-ipex.service
sed -i 's|/path/to/posterchanai|'$(pwd)'|g' posterchanai-ipex.service

# Install and enable
sudo cp posterchanai-ipex.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable posterchanai-ipex
sudo systemctl start posterchanai-ipex
```

---

## Fedora

### 1. Install Intel oneAPI Base Toolkit 2025.0

```bash
cd /tmp
wget https://registrationcenter-download.intel.com/akdlm/IRC_NAS/96aa5993-5b22-4a9b-91ab-da679f422594/intel-oneapi-base-toolkit-2025.0.0.885_offline.sh
chmod +x intel-oneapi-base-toolkit-2025.0.0.885_offline.sh
sudo bash ./intel-oneapi-base-toolkit-2025.0.0.885_offline.sh -a --silent --eula accept
```

### 2. Install Python 3.11 and dependencies

```bash
sudo dnf install python3.11 python3.11-devel gcc gcc-c++
```

### 3. Create IPEX virtual environment

```bash
cd /path/to/posterchanai
python3.11 -m venv venv-ipex
source venv-ipex/bin/activate

# Source oneAPI environment
source /opt/intel/oneapi/2025.0/oneapi-vars.sh

# Install llama-cpp-python with SYCL support
CMAKE_ARGS="-DGGML_SYCL=on -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx" \
    pip install llama-cpp-python==0.3.22 --no-cache-dir

# Install app dependencies
pip install -r requirements.txt
pip install pytz websockets uvicorn[standard]
```

### 4. Create VTune stub library

```bash
cat > /tmp/ittnotify_stub.c << 'EOF'
void __itt_pause(void) {}
void __itt_resume(void) {}
int iJIT_NotifyEvent(int event_type, void *event_data) { return 0; }
void* iJIT_GetNewMethodID(void) { return (void*)0; }
int iJIT_IsProfilingActive(void) { return 0; }
void __itt_thread_set_name(const char* name) {}
void __itt_frame_begin_v3(void* domain, void* id) {}
void __itt_frame_end_v3(void* domain, void* id) {}
void* __itt_domain_create(const char* name) { return (void*)0; }
void* __itt_string_handle_create(const char* name) { return (void*)0; }
void __itt_task_begin(void* domain, void* taskid, void* parentid, void* name) {}
void __itt_task_end(void* domain) {}
EOF

gcc -shared -fPIC -o /tmp/libittnotify.so /tmp/ittnotify_stub.c
sudo cp /tmp/libittnotify.so /usr/local/lib/
sudo ldconfig
```

### 5. Install systemd service

```bash
cp posterchanai-ipex.service.example posterchanai-ipex.service

# Edit paths
sed -i 's|YOUR_USERNAME|'$(whoami)'|g' posterchanai-ipex.service
sed -i 's|/path/to/posterchanai|'$(pwd)'|g' posterchanai-ipex.service

# Install and enable
sudo cp posterchanai-ipex.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable posterchanai-ipex
sudo systemctl start posterchanai-ipex
```

---

## Configuration

After starting the service:

1. Open `http://localhost:3051/admin`
2. Set **Backend Type** to "IPEX-LLM (Intel Arc)"
3. Set **Model Path** to your GGUF model (e.g., `/var/lib/posterchanai/models/Qwen3-14B-abliterated-Q5_K_M.gguf`)
4. Click **Save Settings**
5. Click **Reload Model**

## How It Works

The IPEX backend uses **llama-cpp-python with SYCL** for GGUF models on Intel Arc GPUs:
- SYCL provides native GPU acceleration without executable stack requirements
- Falls back from IPEX-LLM's llama.cpp to standard llama-cpp-python if needed
- Supports Qwen3, Llama, Mistral, and other modern architectures

## Troubleshooting

### "cannot enable executable stack" Error
This error affects IPEX-LLM's PyTorch extension on kernels with `CONFIG_X86_USER_SHADOW_STACK=y`.

**The llama-cpp-python SYCL backend does NOT require executable stack** - it should work fine. If you see this error, the service is falling back to llama-cpp-python automatically.

### "unknown model architecture: qwen3"
You need **llama-cpp-python 0.3.16 or newer** for Qwen3 support (server1 runs 0.3.22). Rebuild with:
```bash
source /opt/intel/oneapi/2025.0/oneapi-vars.sh
CMAKE_ARGS="-DGGML_SYCL=on -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx" \
    pip install llama-cpp-python==0.3.22 --force-reinstall --no-cache-dir
# Do NOT use >=0.3.23: needs oneAPI 2025.1+ (work_group_static.hpp), which breaks IPEX 2.5.10.
```

### "dp4a" or "syclcompat" build errors
You need **oneAPI 2025.0 or newer**. The syclcompat::dp4a function was added in 2025.0.

### `__spirv_GroupBroadcast` / `__spirv_ocl_vloadn` "undefined reference"
These are SPIR-V builtin gaps in **older IGC (2.23.0 / 2.35.2)** — it fails to JIT some llama.cpp
SYCL kernels, which blocks the 14B and surfaces as `Error OP MUL_MAT` / `RMS_NORM`:
```
error: __spirv_GroupBroadcast ... undefined reference
error: __spirv_ocl_vloadn      ... undefined reference
```
**Fix: install IGC 2.35.5** (`sudo ./scripts/install-igc.sh`) — see the GPU-runtime table above.
2.35.5 adds these builtins, so both the 14B/long-context LLM and image gen ≥768 work on one IGC.

Note: with the LLM service still on `llama-cpp-python 0.3.22` + oneAPI 2025.0, the 9B runs fine on
2.35.5. The 14B additionally needs `llama-cpp-python >=0.3.25` built against oneAPI 2025.2 (in its
own venv) — that build is staged but not the default; see the version table / repo history.

### "undefined symbol: iJIT_NotifyEvent"
The VTune stub library wasn't created or isn't being loaded. Verify:
```bash
ls -la /usr/local/lib/libittnotify.so
sudo ldconfig -p | grep ittnotify
```

### Check service logs
```bash
sudo journalctl -u posterchanai-ipex -f
```

### Manual test run
```bash
source /opt/intel/oneapi/2025.0/oneapi-vars.sh
source venv-ipex/bin/activate
export LD_PRELOAD=/usr/local/lib/libittnotify.so
export ZES_ENABLE_SYSMAN=1
python run.py
```

## Image generation (XPU)

Image gen (SDXL via `diffusers`) runs as a **separate service** (`posterchanai-xpu-image.service`,
port 3052) in its **own venv**, on a **modern torch 2.8 XPU** stack — no IPEX, no oneAPI sourcing
(torch 2.8 bundles its own oneAPI 2025.1 runtime via pip). It runs the actual generation in a
subprocess (`scripts/generate_image_subprocess.py`).

**Setup:**
```bash
sudo ./scripts/install-igc.sh        # IGC >=2.35.5 — REQUIRED for SDXL at >=768x768
./scripts/setup-image-instance.sh    # creates venv-xpu-new (torch 2.8 + diffusers), seeds image DB
```
`setup-image-instance.sh` creates **`venv-xpu-new`** and installs `torch==2.8.0` from
`https://download.pytorch.org/whl/xpu` first, then `requirements.txt` + `requirements-image.txt`.
Launcher: `run-xpu-image.sh` (prefers `venv-xpu-new`, sets `LD_LIBRARY_PATH` to the venv libs +
system, does **not** source oneAPI).

**Key constraints (all enforced in `requirements-image.txt`):**
- **IGC ≥ 2.35.5** — at 768/1024 older IGC fails with oneDNN `could not create a primitive`
  (512 still works on old IGC). This is the *same* IGC the LLM 14B needs; one install, both services.
- **`transformers < 5`** — transformers 5.x changed `CLIPTextModel` → diffusers SDXL fails with
  `'CLIPTextModel' object has no attribute 'text_model'`.
- `diffusers >= 0.38`, `accelerate`, `safetensors`, `pillow`.

Proven working: 1024×1024 SDXL ≈ 38s, ~1.7 MB PNG, on IGC 2.35.5 + torch 2.8.0+xpu + diffusers 0.38
+ transformers 4.57.x.

## Supported Models

IPEX-LLM with llama-cpp-python SYCL supports:
- **Qwen3** (0.3.16+)
- **Llama 3.x**
- **Mistral**
- **Phi-3**
- All GGUF quantizations (Q4_K_M, Q5_K_M, Q6_K, Q8_0, etc.)

For best performance on Intel Arc A770 (16GB):
- 14B models: Q4_K_M or Q5_K_M quantization
- 8B models: Q5_K_M or Q8_0 quantization
- Context: 4096-8192 tokens recommended for inference speed

## Performance Notes

Tested with Qwen3-14B-Q5_K_M on Intel Arc A770:
- Model load: ~5.5 seconds
- Prompt processing: ~2.3 tokens/sec
- Generation: ~6 tokens/sec

Performance varies with context length, quantization, and model architecture.
