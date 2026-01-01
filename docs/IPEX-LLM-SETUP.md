# IPEX-LLM Setup Guide

IPEX-LLM provides optimized LLM inference for Intel Arc GPUs. This guide covers installation on Gentoo, Debian/Ubuntu, and Fedora.

## Prerequisites

- Intel Arc GPU (A770, A750, A380, etc.)
- Python 3.11 (required - IPEX-LLM is not compatible with Python 3.12+)
- Intel oneAPI Base Toolkit 2024.2+

---

## Gentoo Linux

### 1. Install Intel oneAPI Base Toolkit

```bash
cd /tmp
wget https://registrationcenter-download.intel.com/akdlm/IRC_NAS/e6ff8e9c-ee28-47fb-abd7-5c524c983e1c/l_BaseKit_p_2024.2.1.100_offline.sh
chmod +x l_BaseKit_p_2024.2.1.100_offline.sh
sudo ./l_BaseKit_p_2024.2.1.100_offline.sh -a --silent --eula accept
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

# Install Intel PyTorch
pip install torch==2.1.0a0 \
    intel-extension-for-pytorch==2.1.30+xpu \
    oneccl_bind_pt==2.1.300+xpu \
    --extra-index-url https://pytorch-extension.intel.com/release-whl/stable/xpu/us/

# Install IPEX-LLM
pip install ipex-llm[xpu]==2.2.0 transformers>=4.37.0

# Install app dependencies
pip install -r requirements.txt
pip install pytz websockets uvicorn[standard]
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
sed -i 's|/opt/intel/oneapi/setvars.sh|/opt/intel/oneapi/2024.2/oneapi-vars.sh|g' posterchanai-ipex.service

# Install and enable
sudo cp posterchanai-ipex.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable posterchanai-ipex
sudo systemctl start posterchanai-ipex
```

---

## Debian / Ubuntu

### 1. Install Intel oneAPI Base Toolkit

```bash
# Add Intel repository
wget -O- https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB | \
    sudo gpg --dearmor -o /usr/share/keyrings/intel.gpg

echo "deb [signed-by=/usr/share/keyrings/intel.gpg] https://apt.repos.intel.com/oneapi all main" | \
    sudo tee /etc/apt/sources.list.d/intel-oneapi.list

sudo apt update
sudo apt install intel-oneapi-base-toolkit
```

### 2. Install Python 3.11 and dependencies

```bash
sudo apt install python3.11 python3.11-venv python3.11-dev gcc
```

### 3. Create IPEX virtual environment

```bash
cd /path/to/posterchanai
python3.11 -m venv venv-ipex
source venv-ipex/bin/activate

# Install Intel PyTorch
pip install torch==2.1.0a0 \
    intel-extension-for-pytorch==2.1.30+xpu \
    oneccl_bind_pt==2.1.300+xpu \
    --extra-index-url https://pytorch-extension.intel.com/release-whl/stable/xpu/us/

# Install IPEX-LLM
pip install ipex-llm[xpu]==2.2.0 transformers>=4.37.0

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

### 1. Install Intel oneAPI Base Toolkit

```bash
# Add Intel repository
sudo tee /etc/yum.repos.d/oneAPI.repo << 'EOF'
[oneAPI]
name=Intel oneAPI repository
baseurl=https://yum.repos.intel.com/oneapi
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=https://yum.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB
EOF

sudo dnf install intel-oneapi-base-toolkit
```

### 2. Install Python 3.11 and dependencies

```bash
sudo dnf install python3.11 python3.11-devel gcc
```

### 3. Create IPEX virtual environment

```bash
cd /path/to/posterchanai
python3.11 -m venv venv-ipex
source venv-ipex/bin/activate

# Install Intel PyTorch
pip install torch==2.1.0a0 \
    intel-extension-for-pytorch==2.1.30+xpu \
    oneccl_bind_pt==2.1.300+xpu \
    --extra-index-url https://pytorch-extension.intel.com/release-whl/stable/xpu/us/

# Install IPEX-LLM
pip install ipex-llm[xpu]==2.2.0 transformers>=4.37.0

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
3. Set **Model Path** to your GGUF or HuggingFace model
4. Click **Save Settings**
5. Click **Reload Model**

## Troubleshooting

### "cannot enable executable stack" Error
Intel's extension library requires an executable stack, which hardened kernels may block.

**Solutions:**
1. Use the wrapper script `run-ipex.sh` which sets `setarch -X` to enable READ_IMPLIES_EXEC
2. If still failing, your kernel may have `CONFIG_X86_USER_SHADOW_STACK=y` which strictly prevents executable stacks
3. **Fallback**: Use the "Native GPU" backend with llama-cpp-python (SYCL) instead - it works without executable stack

To check if your kernel allows executable stack:
```bash
# Check kernel config
zcat /proc/config.gz | grep X86_USER_SHADOW_STACK
# If "=y", executable stack is blocked at kernel level
```

### "undefined symbol: iJIT_NotifyEvent"
The VTune stub library wasn't created or isn't being loaded. Verify:
```bash
ls -la /usr/local/lib/libittnotify.so
sudo ldconfig -p | grep ittnotify
```

### "cannot enable executable stack"
Make sure `setarch` is installed and the service uses the `-X` flag:
```bash
which setarch  # Should show /usr/bin/setarch
```

### Check service logs
```bash
sudo journalctl -u posterchanai-ipex -f
```

### Manual test run
```bash
source /opt/intel/oneapi/setvars.sh  # or oneapi-vars.sh on Gentoo
source venv-ipex/bin/activate
LD_PRELOAD=/usr/local/lib/libittnotify.so setarch $(uname -m) -X python run.py
```

## Supported Models

IPEX-LLM supports:
- **GGUF models** (e.g., Qwen, Llama, Mistral quantized models)
- **HuggingFace models** (loaded with 4-bit quantization)

For best performance on Intel Arc A770 (16GB):
- 14B models: Q4_K_M or Q5_K_M quantization
- 8B models: Q5_K_M or Q8_0 quantization
- Context: 20480-28672 tokens depending on model size
