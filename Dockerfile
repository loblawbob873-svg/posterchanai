# syntax=docker/dockerfile:1.7
# =============================================================================
# PosterChanAI — one Dockerfile, four accelerators (Ubuntu based)
# =============================================================================
# A single build-arg `GPU` selects the compute backend and its base image:
#
#   docker build -t posterchanai:cpu   --build-arg GPU=cpu   .
#   docker build -t posterchanai:cuda  --build-arg GPU=cuda  .   # NVIDIA
#   docker build -t posterchanai:rocm  --build-arg GPU=rocm  .   # AMD
#   docker build -t posterchanai:intel --build-arg GPU=intel .   # Intel Arc / XPU
#
# Run (see docs/DOCKER.md for the full matrix):
#   cpu  : docker run -p 3051:3051 -v pc-data:/var/lib/posterchanai posterchanai:cpu
#   cuda : docker run --gpus all -p 3051:3051 -v pc-data:/var/lib/posterchanai posterchanai:cuda
#   rocm : docker run --device /dev/kfd --device /dev/dri --group-add video -p 3051:3051 posterchanai:rocm
#   intel: docker run --device /dev/dri -p 3051:3051 posterchanai:intel
#
# BuildKit only builds the base stage that `GPU` selects, so the other three
# heavy images are never pulled.
# =============================================================================

# --- base image per accelerator (override the tag with --build-arg if needed) ---
# CUDA and oneAPI ship official toolkit images we build against directly. For AMD
# we install ROCm ourselves from AMD's repos onto plain Ubuntu (AMD's "Docker
# manual install" guide) so the user does not need ROCm preinstalled — only the
# host amdgpu kernel driver is required, so we install ROCm USER-SPACE with no
# DKMS. (To instead build on AMD's prebuilt toolkit image — their "Docker with
# toolkit" guide — pass --build-arg ROCM_BASE=rocm/dev-ubuntu-24.04:6.2.4-complete;
# the repo install below is then a harmless no-op re-add.)
# GPU is global (declared before the first FROM) so `FROM base-${GPU}` resolves;
# the app stage re-declares `ARG GPU` (no default) to reuse this value.
ARG GPU=cpu
ARG CPU_BASE=ubuntu:24.04
ARG CUDA_BASE=nvidia/cuda:12.4.1-devel-ubuntu24.04
ARG ROCM_BASE=ubuntu:24.04
# Use oneAPI 2025.2+ : the 2025.0 SYCL compiler has a codegen bug that makes the Arc emit EMPTY
# generations for thinking-mode/code prompts (verified 2026-06; 2025.2 fixes it and ships
# work_group_static.hpp so no source patches are needed). Override INTEL_BASE if this tag moves.
ARG INTEL_BASE=intel/oneapi-basekit:2025.2.2-0-devel-ubuntu24.04

FROM ${CPU_BASE}   AS base-cpu
FROM ${CUDA_BASE}  AS base-cuda
FROM ${INTEL_BASE} AS base-intel

# AMD: install the ROCm toolkit (userspace, no kernel driver) per
# https://rocm.docs.amd.com/projects/install-on-linux/en/latest/how-to/docker.html
FROM ${ROCM_BASE} AS base-rocm
# ROCm >= 6.3 is required: the current llama.cpp HIP backend uses OCP FP8 types
# (__hip_fp8_e4m3) that don't exist in ROCm 6.2 (the HIP build fails to compile).
ARG ROCM_VERSION=6.3.4
ENV PATH=/opt/rocm/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates && \
    mkdir -p --mode=0755 /etc/apt/keyrings && \
    wget -qO- https://repo.radeon.com/rocm/rocm.gpg.key | gpg --dearmor > /etc/apt/keyrings/rocm.gpg && \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/amdgpu/${ROCM_VERSION}/ubuntu noble main" \
        > /etc/apt/sources.list.d/amdgpu.list && \
    echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/${ROCM_VERSION} noble main" \
        > /etc/apt/sources.list.d/rocm.list && \
    printf 'Package: *\nPin: release o=repo.radeon.com\nPin-Priority: 600\n' > /etc/apt/preferences.d/rocm-pin-600 && \
    apt-get update && apt-get install -y --no-install-recommends rocm && \
    rm -rf /var/lib/apt/lists/*

# Pick the base the build asked for. Everything below is identical across GPUs;
# only the torch / llama-cpp-python install (the `accel` step) differs.
FROM base-${GPU} AS app
ARG GPU

LABEL org.opencontainers.image.title="PosterChanAI" \
      org.opencontainers.image.source="https://github.com/loblawbob873-svg/posterchanai"

# Build-affecting env only (PATH for the venv, apt/pip flags). Runtime env (port,
# caches, turnkey backend defaults) lives LATE in the file so tweaking a default
# doesn't invalidate the heavy apt/torch/llama layers below.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/cuda/bin:$PATH

# --- system packages (all four bases are Ubuntu, so apt is uniform) ----------
#  build : compiler toolchain + cmake for building llama-cpp-python
#  media : ffmpeg (video/TTS + the `hava` effect's image→song MP4), tesseract
#          (OCR), libgl/glib (opencv, PyMuPDF). The Hava Nagila track ships in the
#          repo (assets/hava.mp3) and is baked in by the `COPY . /app` below.
#  fonts : DejaVu Bold + Liberation Sans Bold (meme captions & the Effects
#          text overlays — the BLACKED wordmark prefers the Helvetica-clone
#          Liberation face) + Noto color emoji (screenshots/cards)
#  bt    : system libtorrent (the venv is created with --system-site-packages
#          so the torrent feature can import it)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev python3-pip \
        build-essential cmake git pkg-config patchelf \
        ca-certificates curl gnupg \
        ffmpeg tesseract-ocr \
        libgl1 libglib2.0-0 libjpeg-turbo8 zlib1g \
        fonts-dejavu fonts-liberation fonts-noto-color-emoji fontconfig \
        python3-libtorrent tor \
    && rm -rf /var/lib/apt/lists/*

# --- headless Chrome for the screenshot command (optional, on by default) -----
# The app probes for google-chrome-stable / chromium; Google's .deb is the most
# reliable headless browser in a container (Ubuntu's `chromium` is a snap shim).
ARG INSTALL_BROWSER=true
RUN if [ "$INSTALL_BROWSER" = "true" ]; then \
        curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
            | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg && \
        echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
            > /etc/apt/sources.list.d/google-chrome.list && \
        apt-get update && apt-get install -y --no-install-recommends google-chrome-stable && \
        rm -rf /var/lib/apt/lists/* ; \
    fi

# --- python venv (system-site-packages exposes the apt python3-libtorrent) ----
RUN python3 -m venv --system-site-packages /opt/venv && pip install --upgrade pip setuptools wheel

WORKDIR /app

# --- accelerator-specific PyTorch + llama-cpp-python --------------------------
# torch is installed FIRST from the right wheel index so the requirements.txt
# step (sentence-transformers pulls torch) finds it already satisfied and does
# not drag in a CPU build over it. llama-cpp-python is compiled for the backend.
ARG TORCH_CUDA_INDEX=https://download.pytorch.org/whl/cu121
ARG TORCH_ROCM_INDEX=https://download.pytorch.org/whl/rocm6.3
ARG TORCH_XPU_INDEX=https://download.pytorch.org/whl/xpu
ARG TORCH_XPU_VERSION=2.12.0
# LLAMA_CPP_VERSION empty = latest (the Intel branch pins 0.3.22 below).
ARG LLAMA_CPP_VERSION=
# AMDGPU_TARGETS: which HIP GPU arches to build llama.cpp kernels for. Defaults to
# common consumer RDNA2/RDNA3 (RX 6000/7000) so the image runs on most AMD cards
# out of the box; narrow it to just your card for a faster, smaller build.
ARG AMDGPU_TARGETS=gfx1030;gfx1031;gfx1100;gfx1101;gfx1102

RUN set -eux; \
    case "$GPU" in \
      cpu) \
        pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu ; \
        CMAKE_ARGS="-DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=ON" \
          pip install "llama-cpp-python${LLAMA_CPP_VERSION:+==$LLAMA_CPP_VERSION}" ; \
        ;; \
      cuda) \
        pip install torch torchvision --index-url "$TORCH_CUDA_INDEX" ; \
        CMAKE_ARGS="-DGGML_CUDA=ON" \
          pip install "llama-cpp-python${LLAMA_CPP_VERSION:+==$LLAMA_CPP_VERSION}" ; \
        ;; \
      rocm) \
        pip install torch torchvision --index-url "$TORCH_ROCM_INDEX" ; \
        ( HIP_PATH=/opt/rocm ROCM_PATH=/opt/rocm \
          CMAKE_ARGS="-DGGML_HIP=ON -DAMDGPU_TARGETS=${AMDGPU_TARGETS}" \
            pip install "llama-cpp-python${LLAMA_CPP_VERSION:+==$LLAMA_CPP_VERSION}" ) \
        || pip install "llama-cpp-python${LLAMA_CPP_VERSION:+==$LLAMA_CPP_VERSION}" ; \
        ;; \
      intel) \
        pip install "torch==${TORCH_XPU_VERSION}" torchvision --index-url "$TORCH_XPU_INDEX" \
          || pip install "torch==${TORCH_XPU_VERSION}" --index-url "$TORCH_XPU_INDEX" ; \
        bash -c 'source /opt/intel/oneapi/setvars.sh >/dev/null 2>&1; \
          CMAKE_ARGS="-DGGML_SYCL=ON -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx" \
            pip install "llama-cpp-python==${LLAMA_CPP_VERSION:-0.3.22}"' \
        || pip install "llama-cpp-python==${LLAMA_CPP_VERSION:-0.3.22}" ; \
        ;; \
      *) echo "Unknown GPU '$GPU' (use cpu|cuda|rocm|intel)"; exit 1 ;; \
    esac

# --- application python deps --------------------------------------------------
# Copy just the requirement files first so this layer caches across source edits.
# requirements.txt = the web app (FastAPI, Pillow, chromadb, sentence-transformers,
# faster-whisper, insightface/onnxruntime …). botframework adds the bot deps.
# diffusers stack = native image generation (transformers pinned <5 for SDXL).
COPY requirements.txt /tmp/requirements.txt
COPY botframework/requirements.txt /tmp/requirements-bot.txt
RUN pip install -r /tmp/requirements.txt -r /tmp/requirements-bot.txt \
    && pip install "transformers<5" diffusers accelerate safetensors huggingface_hub

# --- app source ---------------------------------------------------------------
COPY . /app

# Runtime data lives on a volume: uploads, downloaded models, RAG store, sqlite db,
# HF caches. The entrypoint symlinks the db onto the volume so it persists too.
RUN mkdir -p /var/lib/posterchanai/models /var/lib/posterchanai/torrents \
             /var/lib/posterchanai/tor /var/lib/posterchanai/hf /app/data/chromadb
VOLUME ["/var/lib/posterchanai", "/app/data"]

# Runtime config (LATE so changing a default is a cheap rebuild). HF_HOME caches
# models on the data volume. Turnkey defaults: every GPU/CPU build ships a locally
# compiled llama-cpp + torch/diffusers, so the app uses the `native` LLM AND image
# backends. The entrypoint auto-downloads the recommended GGUF on first run
# (DOWNLOAD_MODEL=1, ~5.6 GB); diffusers fetches the image model (DreamShaper-8, an
# SD1.5 model — fast and fits alongside the LLM on consumer GPUs) on first gen.
# These only SEED first-run settings (app/database.py); change them in the admin UI
# afterwards. Set DOWNLOAD_MODEL=0 to skip the LLM pull. For a bigger image model
# (e.g. SDXL) on a small GPU, set POSTERCHANAI_LOW_VRAM=1 to enable model offload.
# Persist the build accelerator so the entrypoint can apply per-GPU runtime settings
# (e.g. Intel: the SYCL device selector + subprocess-per-image VRAM release).
ENV PC_ACCEL=${GPU}
ENV POSTERCHANAI_PORT=3051 \
    HF_HOME=/var/lib/posterchanai/hf \
    MIOPEN_USER_DB_PATH=/var/lib/posterchanai/miopen \
    MIOPEN_CUSTOM_CACHE_DIR=/var/lib/posterchanai/miopen \
    MIOPEN_FIND_MODE=2 \
    DOWNLOAD_MODEL=1 \
    DOWNLOAD_DEPTH_MODEL=1 \
    DEPTH_MODEL_PATH=/var/lib/posterchanai/assets/depth_anything_v2_vits.onnx \
    DEPTH_MODEL_URL=https://huggingface.co/onnx-community/depth-anything-v2-small/resolve/main/onnx/model.onnx \
    POSTERCHANAI_LLM_MODEL_PATH=/var/lib/posterchanai/models/Qwen3.5-9B-abliterated-Q4_K_M.gguf \
    POSTERCHANAI_MODEL_URL=https://huggingface.co/lukey03/Qwen3.5-9B-abliterated-GGUF/resolve/main/Qwen3.5-9B-abliterated-Q4_K_M.gguf \
    POSTERCHANAI_IMAGE_MODEL_PATH=Lykon/dreamshaper-8 \
    POSTERCHANAI_IMAGE_MODEL_TYPE=sd15
# Tor / built-in proxy / torrenting ship ready (the `tor` binary + libtorrent are
# installed) but stay OFF by default — enable them in the admin UI, or seed them on
# at first run with -e POSTERCHANAI_TOR_ENABLED=true -e POSTERCHANAI_PROXY_ENABLED=true
# -e POSTERCHANAI_BT_ENABLED=true (the app then starts Tor + the HTTP proxy itself).

EXPOSE 3051

# TCP health check on the configured port (the UI redirects to /login, so a plain
# socket connect is a cleaner liveness probe than an HTTP status check).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python3 -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('POSTERCHANAI_PORT','3051'))), 5)" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "run.py"]
