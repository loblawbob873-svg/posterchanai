# Intel Arc Setup Guide

> ## ⚡ UPDATED (2026-06): Unified stack — IPEX-LLM is no longer used
>
> Intel Arc now runs **chat + image generation from ONE venv and ONE service**, just like the
> NVIDIA/AMD backends. The old split (a `venv-ipex` IPEX-LLM chat service on :3051 + a
> `venv-xpu`/`venv-xpu-new` image service on :3052) is **gone** — Intel dropped IPEX-LLM
> development after torch 2.8, and it couldn't share a process with torch-XPU image gen anyway.
>
> **Current Intel stack (what `install.sh` builds now):**
>
> | Component | Version / value | Notes |
> |-----------|-----------------|-------|
> | venv | **`venv-unified`** | ONE venv: chat **and** image |
> | torch | **2.12.0+xpu** | native PyTorch-XPU; bundles its own oneAPI runtime (no system oneAPI, no IPEX) |
> | chat backend | **`llama-cpp-python` (SYCL)** | built `-DGGML_SYCL=ON` (the only local LLM backend — Ollama/IPEX removed) |
> | image backend | **diffusers (torch-XPU)** | the only image backend (ComfyUI removed); per-gen subprocess (`image_subprocess_mode=true`) frees VRAM on the shared GPU |
> | IGC (system) | **>= 2.35.5** | `scripts/install-igc.sh`; still required for SDXL ≥768 |
> | launcher | **`run-intel.sh`** | sets `LD_LIBRARY_PATH=venv-unified/lib:/usr/lib64` + **`ONEAPI_DEVICE_SELECTOR=level_zero:gpu`** |
>
> **`ONEAPI_DEVICE_SELECTOR=level_zero:gpu` is mandatory** — without it llama.cpp SYCL silently
> selects the CPU device (~2 tok/s instead of ~19). `run-intel.sh` does **not** source a system
> oneAPI; torch 2.12's bundled runtime serves both backends (mixing in a system oneAPI causes a
> `LIBUR_LOADER` symbol mismatch). Docker: `--build-arg GPU=intel` bakes this in and the
> entrypoint applies the same env when `PC_ACCEL=intel`.
>
> **Everything below is the LEGACY IPEX-LLM guide, kept for reference / older installs only.**

---

# IPEX-LLM Setup Guide (LEGACY)

IPEX-LLM provided optimized LLM inference for Intel Arc GPUs. This guide covers installation on Gentoo, Debian/Ubuntu, and Fedora.

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

## Install responsibilities: what the OS provides vs what the installer handles

For an Intel Arc box, three layers must be in place. **You** install the first via your distro;
**`install.sh`** (and the helper scripts) handle the rest.

| Layer | Who | What |
|------|-----|------|
| **1. OS GPU runtime** (distro pkgs) | **You** (distro pkg manager) | `intel-compute-runtime` (NEO), `level-zero`/`libze1`, `gmmlib`/`libigdgmm`, plus build tools `python3.11`, `cmake`, `gcc`/`gcc-c++`, `git`, `patchelf`, `pax-utils`. Pin these so updates don't downgrade them. |
| **1b. oneAPI Base Toolkit 2025.0** | **You** | Needed only to *build* the SYCL `llama-cpp-python` (the LLM venv). Image gen does **not** need it. Install from Intel's repo/toolkit. |
| **1c. IGC 2.35.5** | **`scripts/install-igc.sh`** | Distro IGC is too old (≤2.35.2). Run `sudo ./scripts/install-igc.sh [--download]` once — distro-agnostic, installs the 4 libs (incl. `libopencl-clang2.so.17`) with a backup. Unblocks the 14B/long-context **and** image gen ≥768. |
| **2. LLM venv** (`venv-ipex`) | **`install.sh`** | Python 3.11, `llama-cpp-python==0.3.22` built with SYCL (icx/icpx), ipex-llm, `requirements.txt`. |
| **3. Image venv** (`venv-xpu`) | **`install.sh`** | `torch==2.8.0` XPU (bundles its own oneAPI — no IPEX), `requirements-image.txt` (diffusers≥0.38, transformers<5), `requirements.txt`. |

### Per-distro: the OS GPU-runtime packages you install (layer 1)

| Distro | Command |
|--------|---------|
| **Gentoo** | `emerge -av dev-libs/intel-compute-runtime dev-libs/level-zero media-libs/gmmlib dev-util/patchelf app-misc/pax-utils` (pin `~amd64`; add `dev-util/intel-graphics-compiler no-distcc.conf` to `package.env`) |
| **Debian/Ubuntu** | Intel GPU apt repo (see [dgpu-docs](https://dgpu-docs.intel.com/driver/installation.html)): `apt install intel-opencl-icd libze-intel-gpu1 libze1 intel-level-zero-gpu libigdgmm12 patchelf pax-utils` |
| **Fedora** | `dnf install intel-compute-runtime oneapi-level-zero level-zero intel-gmmlib patchelf pax-utils` |
| **openSUSE** | `zypper install intel-compute-runtime level-zero libze1 libigdgmm12 patchelf pax-utils` (Tumbleweed; Leap may need Intel's repo) |
| **Arch** | `pacman -S intel-compute-runtime level-zero-loader intel-graphics-compiler patchelf pax-utils` (oneAPI from AUR `intel-oneapi-basekit`) |

After layer 1 + oneAPI + `install-igc.sh`, run `./install.sh` and it builds the ONE venv
(`venv-unified`, chat + image together) and the `posterchanai` service on :3051. There is no second
image venv or image service; :3052 is the built-in Nostr relay. The installer
auto-detects the distro (`/etc/os-release`, incl. `ID_LIKE` for derivatives) and prints the exact
package commands for anything still missing.

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
# The installer (./install.sh) creates this for you. Manual version below — the unit is generic
# because all the Intel/oneAPI setup lives in run-intel.sh.
sudo tee /etc/systemd/system/posterchanai.service >/dev/null <<EOF
[Unit]
Description=Posterchan AI (Intel Arc GPU, llama-cpp SYCL)
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$(pwd)
ExecStart=$(pwd)/run-intel.sh
Restart=always
RestartSec=3
TimeoutStartSec=120
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now posterchanai
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
cp posterchanai.service.example posterchanai.service

# Edit paths
sed -i 's|YOUR_USERNAME|'$(whoami)'|g' posterchanai.service
sed -i 's|/path/to/posterchanai|'$(pwd)'|g' posterchanai.service

# Install and enable
sudo cp posterchanai.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable posterchanai
sudo systemctl start posterchanai
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
cp posterchanai.service.example posterchanai.service

# Edit paths
sed -i 's|YOUR_USERNAME|'$(whoami)'|g' posterchanai.service
sed -i 's|/path/to/posterchanai|'$(pwd)'|g' posterchanai.service

# Install and enable
sudo cp posterchanai.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable posterchanai
sudo systemctl start posterchanai
```

---

## openSUSE (Leap / Tumbleweed)

openSUSE follows the same flow as Fedora/Debian — only the package manager differs. The installer
detects it via `/etc/os-release` (`ID=opensuse*` / `ID_LIKE=suse`) and uses `zypper`.

### 1. OS GPU runtime + build tools
```bash
sudo zypper install python311 python311-pip cmake gcc-c++ git patchelf pax-utils \
    intel-compute-runtime level-zero libze1 libigdgmm12
# Tumbleweed ships current packages; Leap may need Intel's GPU repo (dgpu-docs.intel.com).
```

### 2. oneAPI Base Toolkit 2025.0 (to build the SYCL llama-cpp)
Add Intel's oneAPI zypper repo, then `sudo zypper install intel-basekit` (installs to
`/opt/intel/oneapi`). Image gen does not need this.

### 3. IGC 2.35.5 (required — distro IGC is too old)
```bash
sudo ./scripts/install-igc.sh --download
```

### 4. Run the installer
```bash
./install.sh    # builds venv-ipex (LLM) + venv-xpu (image, torch 2.8) + both systemd services
```
Steps 4–6 (VTune stub, venvs, systemd) are identical to the Fedora section — the installer handles
them. The VTune stub (`libittnotify.so`) is created automatically; if you do it by hand, drop it in
`/usr/local/lib` and `ldconfig`.

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
sudo journalctl -u posterchanai -f
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

Image gen (SDXL via `diffusers`) runs **inside the main `posterchanai` service** on the unified
venv, on a **modern torch 2.8 XPU** stack — no IPEX, no oneAPI sourcing (torch 2.8 bundles its own
oneAPI 2025.1 runtime via pip). It can run the generation itself or fork it
(`scripts/generate_image_subprocess.py`, the `image_subprocess_mode` setting).

There is **no `posterchanai-xpu-image.service` and no port-3052 image server** — that pair was
retired when the venvs were unified, and :3052 is the Nostr relay now. Both are still worth saying
out loud because a stale reference to a service that no longer exists costs a real investigation:
`systemctl is-active` reports `inactive` for a unit that was never installed, so the absence reads
as an outage.

**Setup (legacy — superseded by the unified `install.sh` flow at the top of this doc):**
```bash
sudo ./scripts/install-igc.sh        # IGC >=2.35.5 — REQUIRED for SDXL at >=768x768
```
The separate `venv-xpu`/`venv-xpu-new` image instance and `setup-image-instance.sh` have been
removed. Image generation now lives in the unified `venv-unified` (see the banner at the top),
running as a per-gen subprocess (`scripts/generate_image_subprocess.py`).

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
