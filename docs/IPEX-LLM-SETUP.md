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

### OS-level Intel GPU runtime (Gentoo `emerge`, not pip)
This layer is **separate from Python** and is what fixes long-context empty/garbage on the A770:

| Package | Version | Notes |
|---------|---------|-------|
| dev-util/intel-graphics-compiler | **2.35.2** | ⭐ the fix — older 2.23.0 produced empty output on long-context steps |
| dev-libs/intel-compute-runtime | **25.40.35563.4** | Keep on 25.40 — **26.x will not build** (gmmlib→IGC `__spirv_ocl_vloadn` skew) |
| dev-libs/level-zero | **1.28.6** | |
| media-libs/gmmlib | **22.10.0** | |

All four are `~amd64`; pin them in `/etc/portage/package.accept_keywords` or `@world` will downgrade.
Arc DB settings: `llm_flash_attn=false` (flash-attn is broken on Arc SYCL even with IGC 2.35.2), `llm_n_batch=1024`.

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

### `__spirv_GroupBroadcast` / `__spirv_ocl_vloadn` "undefined reference" — IGC 2.35.2 kernel-compile gap
The newer `intel-graphics-compiler 2.35.2` (pinned because it fixes the A770 long-context
empty/garbage — see GPU-runtime table above) has **SPIR-V builtin gaps**: it fails to JIT-compile
*some* llama.cpp SYCL kernels at runtime, e.g.:
```
error: __spirv_GroupBroadcast ... undefined reference   (OP RMS_NORM_back)
error: __spirv_ocl_vloadn      ... undefined reference   (copy kernels)
```
**Inference still works in production** — the *forward* kernels the chat path needs DO compile and
are cached in `~/.cache/neo_compiler_cache`. The failures are on kernels the inference path doesn't
use (e.g. `rms_norm_back`, a training op). Practical consequences:
- Don't be alarmed by these `__spirv_*` errors in logs for unused kernels.
- A **fresh standalone llama-cpp load outside the service may fail to init** (it can trip an
  uncompilable kernel) even though the systemd service runs fine.
- **Do not delete `~/.cache/neo_compiler_cache`** casually — it holds the working compiled kernels.
- The fully-clean fix is the coordinated upgrade (oneAPI 2025.1+ / matching IPEX / llama-cpp ≥0.3.23),
  which is deferred because it breaks `ipex 2.5.10`. Until then, IGC 2.35.2 is the accepted trade-off:
  correct long-context output, with these builtin gaps on unused kernels.

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
