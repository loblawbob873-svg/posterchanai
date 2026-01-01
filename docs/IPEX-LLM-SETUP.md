# IPEX-LLM Setup Guide

IPEX-LLM provides optimized LLM inference for Intel Arc GPUs. This guide covers installation on Gentoo, Debian/Ubuntu, and Fedora.

## Prerequisites

- Intel Arc GPU (A770, A750, A380, etc.)
- Python 3.11 (required - IPEX-LLM is not compatible with Python 3.12+)
- **Intel oneAPI Base Toolkit 2025.0+** (required for llama-cpp-python SYCL backend with Qwen3 support)

## Version Requirements

| Component | Version | Notes |
|-----------|---------|-------|
| Intel oneAPI Base Toolkit | **2025.0.0** or newer | Required for SYCL syclcompat::dp4a |
| Python | 3.11.x | 3.12+ not supported |
| llama-cpp-python | **0.3.16** | Built with SYCL for Intel GPU |
| ipex-llm | 2.2.0+ | Optional, for HuggingFace models |

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
    pip install llama-cpp-python==0.3.16 --no-cache-dir

# Install app dependencies
pip install -r requirements.txt
pip install pytz websockets uvicorn[standard]

# Optional: Install IPEX-LLM for HuggingFace models
pip install torch==2.1.0a0 \
    intel-extension-for-pytorch==2.1.30+xpu \
    oneccl_bind_pt==2.1.300+xpu \
    --extra-index-url https://pytorch-extension.intel.com/release-whl/stable/xpu/us/
pip install ipex-llm[xpu]==2.2.0 transformers>=4.37.0
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
    pip install llama-cpp-python==0.3.16 --no-cache-dir

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
    pip install llama-cpp-python==0.3.16 --no-cache-dir

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
You need **llama-cpp-python 0.3.16 or newer** for Qwen3 support. Rebuild with:
```bash
source /opt/intel/oneapi/2025.0/oneapi-vars.sh
CMAKE_ARGS="-DGGML_SYCL=on -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx" \
    pip install llama-cpp-python==0.3.16 --force-reinstall --no-cache-dir
```

### "dp4a" or "syclcompat" build errors
You need **oneAPI 2025.0 or newer**. The syclcompat::dp4a function was added in 2025.0.

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
