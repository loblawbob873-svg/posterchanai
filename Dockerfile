# syntax=docker/dockerfile:1.7
# =============================================================================
# PosterChanAI — one Dockerfile, four accelerators + a Nostr-only build (Ubuntu based)
# =============================================================================
# A single build-arg `GPU` selects the compute backend and its base image:
#
#   docker build -t posterchanai:cpu   --build-arg GPU=cpu   .
#   docker build -t posterchanai:cuda  --build-arg GPU=cuda  --build-arg BASE_IMAGE=nvidia/cuda:12.5.1-devel-ubuntu24.04 .   # NVIDIA
#   docker build -t posterchanai:rocm  --build-arg GPU=rocm  .   # AMD (ROCm userspace installed onto ubuntu:24.04)
#   docker build -t posterchanai:intel --build-arg GPU=intel --build-arg BASE_IMAGE=intel/oneapi-basekit:2025.2.2-0-devel-ubuntu24.04 .   # Intel Arc / XPU
#   docker build -t posterchanai:nostr --build-arg GPU=nostr --build-arg INSTALL_BROWSER=false .
# (BASE_IMAGE defaults to ubuntu:24.04, so cpu/nostr/rocm need only GPU; cuda/intel must pass it.)
#                                                # Nostr-only: relay + Nostr web client + Blossom,
#                                                # NO AI (no torch/llama/diffusers) → small image.
#
# Run (see docs/DOCKER.md for the full matrix):
#   cpu  : docker run -p 3051:3051 -v pc-data:/var/lib/posterchanai posterchanai:cpu
#   cuda : docker run --gpus all -p 3051:3051 -v pc-data:/var/lib/posterchanai posterchanai:cuda
#   rocm : docker run --device /dev/kfd --device /dev/dri --group-add video -p 3051:3051 posterchanai:rocm
#   intel: docker run --device /dev/dri -p 3051:3051 posterchanai:intel
#
# ONE base FROM, parametrized by `BASE_IMAGE`, so a cpu/nostr build never references the
# cuda/intel/rocm images — they are NOT pulled even on the legacy (non-BuildKit) builder, which
# builds every FROM stage it can SEE (the old per-GPU `base-*` stages + `FROM base-${GPU}` only
# avoided the extra pulls under BuildKit). The compose file passes the right base per profile.
# =============================================================================

# --- base image per accelerator (the compose file sets BASE_IMAGE per profile) ---
# CUDA and oneAPI ship official toolkit images we build against directly; for AMD we stay on plain
# Ubuntu and install ROCm USER-SPACE ourselves below (no DKMS — only the host amdgpu kernel driver
# is needed). Standalone `docker build` must pass BASE_IMAGE to match GPU (see the header examples):
#   cpu / nostr : ubuntu:24.04
#   cuda        : nvidia/cuda:12.5.1-devel-ubuntu24.04
#   intel       : intel/oneapi-basekit:2025.2.2-0-devel-ubuntu24.04  (oneAPI 2025.2+ — the 2025.0
#                 SYCL compiler has a codegen bug that makes the Arc emit EMPTY thinking/code gens)
#   rocm        : ubuntu:24.04   (or AMD's prebuilt rocm/dev-ubuntu-24.04:*-complete — the repo
#                 install below is then a harmless no-op re-add)
ARG GPU=cpu
ARG BASE_IMAGE=ubuntu:24.04
# ROCm >= 6.3 is required: the current llama.cpp HIP backend uses OCP FP8 types
# (__hip_fp8_e4m3) that don't exist in ROCm 6.2 (the HIP build fails to compile).
ARG ROCM_VERSION=6.3.4

# --- Go build stage: the built-in Pion TURN/STUN relay for voice/video calls (tiny static binary) ---
FROM golang:1.23-alpine AS turnbuild
WORKDIR /build
COPY turnserver/go.mod turnserver/go.sum ./
RUN go mod download
COPY turnserver/ ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o pion-turn .

# --- Download stage: the built-in MediaMTX media server for OBS streaming (prebuilt binary, no build) ---
FROM alpine:3.20 AS streamdl
ARG TARGETARCH
ARG MEDIAMTX_VERSION=v1.19.2
# Best-effort: a yanked/renamed upstream release must NOT break the whole image build. On failure we
# leave an empty /mediamtx placeholder; stream_service treats a 0-byte binary as "not installed" (no-op),
# and the operator can install it later via install.sh --stream. Streaming is opt-in anyway.
RUN apk add --no-cache curl tar && { \
      case "${TARGETARCH:-amd64}" in \
        amd64) MTXARCH=amd64 ;; arm64) MTXARCH=arm64 ;; arm) MTXARCH=armv7 ;; *) MTXARCH=amd64 ;; \
      esac && \
      curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_${MTXARCH}.tar.gz" -o /tmp/m.tgz && \
      tar -xzf /tmp/m.tgz -C /tmp mediamtx && install -m 0755 /tmp/mediamtx /mediamtx ; \
    } || { echo "WARN: mediamtx download failed — streaming disabled in this image (install.sh --stream to add it)"; : > /mediamtx ; }

FROM ${BASE_IMAGE} AS app
ARG GPU
ARG ROCM_VERSION

# AMD only: install the ROCm USERSPACE toolkit onto the Ubuntu base (the kernel driver comes from
# the host). Guarded on GPU=rocm so no other build runs it — and since it's a plain RUN (not a
# separate base stage), it's never even parsed for a cpu/nostr/cuda/intel build.
# https://rocm.docs.amd.com/projects/install-on-linux/en/latest/how-to/docker.html
RUN if [ "$GPU" = "rocm" ]; then set -eux; \
        apt-get update && apt-get install -y --no-install-recommends wget gnupg ca-certificates && \
        mkdir -p --mode=0755 /etc/apt/keyrings && \
        wget -qO- https://repo.radeon.com/rocm/rocm.gpg.key | gpg --dearmor > /etc/apt/keyrings/rocm.gpg && \
        echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/amdgpu/${ROCM_VERSION}/ubuntu noble main" \
            > /etc/apt/sources.list.d/amdgpu.list && \
        echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/${ROCM_VERSION} noble main" \
            > /etc/apt/sources.list.d/rocm.list && \
        printf 'Package: *\nPin: release o=repo.radeon.com\nPin-Priority: 600\n' > /etc/apt/preferences.d/rocm-pin-600 && \
        apt-get update && apt-get install -y --no-install-recommends rocm && \
        rm -rf /var/lib/apt/lists/* ; \
    fi
# Harmless for non-AMD (a non-existent dir on PATH); the later venv ENV keeps this via $PATH.
ENV PATH=/opt/rocm/bin:$PATH

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
#  bt    : tor + geoip only. libtorrent is NOT an apt package here — it comes from
#          requirements.txt as a prebuilt manylinux wheel that statically bundles boost, so it
#          matches the venv's ABI and is newer (2.0.13) than Ubuntu's python3-libtorrent (2.0.10),
#          which pip's copy shadowed anyway. See requirements.txt for the wheel's Python range.
# NOTE: the per-user Debian SANDBOX (Admin → node_exec_sandbox_enabled, OFF by default) shells out to
# the `docker` CLI (docker-outside-of-docker). It is NOT installed here to keep the image lean — add
# `docker.io` (or docker-ce-cli) to the list below AND mount /var/run/docker.sock + `group_add` in
# docker-compose.yml if you want the sandbox inside the container deployment. Bare-metal: ./install.sh --sandbox.
# `git` is not just a build tool here: the built-in GRASP git server (POSTERCHANAI_GIT=1) execs
# `git-http-backend` (shipped by the same package, at /usr/lib/git-core on Debian/Ubuntu) and runs
# git plumbing for the web file browser/editor. Removing it from this list breaks docs/GIT.md.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev python3-pip \
        build-essential cmake git pkg-config patchelf \
        ca-certificates curl gnupg \
        ffmpeg tesseract-ocr \
        tesseract-ocr-tha tesseract-ocr-chi-sim tesseract-ocr-chi-tra tesseract-ocr-jpn \
        tesseract-ocr-kor tesseract-ocr-ara tesseract-ocr-rus tesseract-ocr-hin \
        tesseract-ocr-spa tesseract-ocr-fra tesseract-ocr-deu \
        libgl1 libglib2.0-0 libjpeg-turbo8 zlib1g \
        fonts-dejavu fonts-liberation fonts-noto-color-emoji fontconfig \
        tor tor-geoipdb \
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

# --- python venv -------------------------------------------------------------
# ISOLATED on purpose (no --system-site-packages). That flag existed solely to expose the apt
# python3-libtorrent; the pip wheel replaces it, and leaking every apt python3 package into the venv
# is a standing footgun — a distro-packaged module can silently shadow the pip one the app pinned.
RUN python3 -m venv /opt/venv && pip install --upgrade pip setuptools wheel

WORKDIR /app

# --- accelerator-specific PyTorch + llama-cpp-python --------------------------
# torch is installed FIRST from the right wheel index so the later diffusers
# step (transformers pulls torch) finds it already satisfied and does
# not drag in a CPU build over it. llama-cpp-python is compiled for the backend.
ARG TORCH_CUDA_INDEX=https://download.pytorch.org/whl/cu121
ARG TORCH_ROCM_INDEX=https://download.pytorch.org/whl/rocm6.3
ARG TORCH_XPU_INDEX=https://download.pytorch.org/whl/xpu
ARG TORCH_XPU_VERSION=2.12.0
# LLAMA_CPP_VERSION empty = latest. Intel branch pins 0.3.28 below (built with the 2025.2 base's
# icx/icpx — fixes the 2025.0 SYCL codegen empty-gen bug; 2025.2 ships the headers 0.3.28 needs).
ARG LLAMA_CPP_VERSION=
# AMDGPU_TARGETS: which HIP GPU arches to build llama.cpp kernels for. Defaults to
# common consumer RDNA2/RDNA3 (RX 6000/7000) so the image runs on most AMD cards
# out of the box; narrow it to just your card for a faster, smaller build.
ARG AMDGPU_TARGETS=gfx1030;gfx1031;gfx1100;gfx1101;gfx1102

RUN set -eux; \
    case "$GPU" in \
      nostr) \
        echo "Nostr-only build: skipping torch / llama-cpp-python / diffusers (no AI features)" ; \
        ;; \
      cpu) \
        pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu ; \
        CMAKE_ARGS="-DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=ON" \
          pip install "llama-cpp-python${LLAMA_CPP_VERSION:+==$LLAMA_CPP_VERSION}" ; \
        ;; \
      cuda) \
        pip install torch torchvision --index-url "$TORCH_CUDA_INDEX" ; \
        # The devel image ships a BUILD-TIME stub libcuda (the real driver lib is mounted at runtime
        # by the nvidia container runtime). Newer llama.cpp links the CUDA driver API (cuMem*) so the
        # link fails with "libcuda.so.1 not found" unless we expose the stub as .so.1 + on LIBRARY_PATH.
        ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/local/cuda/lib64/stubs/libcuda.so.1 ; \
        LIBRARY_PATH="/usr/local/cuda/lib64/stubs:${LIBRARY_PATH:-}" \
        CMAKE_ARGS="-DGGML_CUDA=ON -DCMAKE_SHARED_LINKER_FLAGS=-Wl,-rpath-link,/usr/local/cuda/lib64/stubs -DCMAKE_EXE_LINKER_FLAGS=-Wl,-rpath-link,/usr/local/cuda/lib64/stubs" \
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
            pip install "llama-cpp-python==${LLAMA_CPP_VERSION:-0.3.28}"' \
        || pip install "llama-cpp-python==${LLAMA_CPP_VERSION:-0.3.28}" ; \
        ;; \
      *) echo "Unknown GPU '$GPU' (use nostr|cpu|cuda|rocm|intel)"; exit 1 ;; \
    esac

# --- application python deps --------------------------------------------------
# Copy just the requirement files first so this layer caches across source edits.
# requirements.txt = the web app (FastAPI, Pillow,
# faster-whisper, insightface/onnxruntime …). botframework adds the bot deps.
# diffusers stack = native image generation (transformers pinned <5 for SDXL) AND text-to-video
# (videogeni — Wan2.1/LTX/CogVideoX); sentencepiece is REQUIRED for the T5 video text-encoder.
COPY requirements.txt /tmp/requirements.txt
COPY requirements-nostr.txt /tmp/requirements-nostr.txt
COPY botframework/requirements.txt /tmp/requirements-bot.txt
# Nostr-only (GPU=nostr) installs the lean Nostr/web requirements and SKIPS the diffusers/
# transformers image-gen stack entirely; all other builds get the full app + image-gen deps.
# IMPORTANT: the Nostr stack (relay client + signing + datastore: coincurve, websockets,
# websocket-client, segno, cryptography) is CRITICAL in EVERY image, not just the nostr-only one.
# requirements.txt is therefore a SUPERSET of requirements-nostr.txt (full app + AI + Nostr); the
# lean file just drops the AI extras. Keep those Nostr deps in BOTH files — never move one into
# requirements-nostr.txt only, or the cpu/cuda/rocm/intel images would lose Nostr support.
# NOTE: the nostr branch must FAIL LOUDLY — no `2>/dev/null` and no `|| <full requirements>`
# fallback. A swallowed error there used to silently install the full AI stack (torch/onnx/cuda),
# which is exactly what a lean "no-AI" image is meant to avoid. If a lean dep won't install, stop.
RUN if [ "$GPU" = "nostr" ]; then \
        pip install -r /tmp/requirements-nostr.txt ; \
    else \
        pip install -r /tmp/requirements.txt -r /tmp/requirements-bot.txt \
          && pip install "numpy>=2,<2.5" "transformers<5" diffusers accelerate safetensors huggingface_hub sentencepiece ftfy ; \
    fi

# --- music generation (musicgeni), IN-PROCESS ---------------------------------
# ACE-Step now generates inside the app process, on the torch installed above — there is no separate
# acestep container, no second venv and no HTTP hop (Dockerfile.acestep is retired). Opt in with
# --build-arg INSTALL_MUSIC=1; the model itself downloads on first request into the HF cache volume.
#
# --no-deps is LOAD-BEARING. ACE-Step's pyproject pins torch==2.10.0+cu128 and gradio; resolving
# those would replace the GPU torch installed above and break image gen in the same image. Its real
# runtime deps ship in requirements.txt. torchaudio is resolved from the SAME index as torch for the
# same reason — a bare `pip install torchaudio` pulls a CPU torch in behind it.
ARG INSTALL_MUSIC=0
ARG ACESTEP_REF=main
RUN if [ "$INSTALL_MUSIC" = "1" ] && [ "$GPU" != "nostr" ]; then \
      set -eu; \
      case "$GPU" in \
        intel) _TA_INDEX="$TORCH_XPU_INDEX" ;; \
        rocm)  _TA_INDEX="$TORCH_ROCM_INDEX" ;; \
        cuda)  _TA_INDEX="$TORCH_CUDA_INDEX" ;; \
        *)     _TA_INDEX="https://download.pytorch.org/whl/cpu" ;; \
      esac; \
      pip install --no-deps torchaudio --index-url "$_TA_INDEX"; \
      git clone --depth 1 --branch "$ACESTEP_REF" https://github.com/ace-step/ACE-Step-1.5.git /opt/ace-step; \
      pip install --no-deps -e /opt/ace-step; \
      python3 -c 'from acestep.handler import AceStepHandler' ; \
    fi
ENV ACESTEP_ROOT=/opt/ace-step

# --- app source ---------------------------------------------------------------
COPY . /app

# Normalise line endings on anything the container EXECUTES. Git for Windows checks out CRLF by default,
# which turns the shebang into `#!/usr/bin/env bash\r` — the kernel then looks for a binary literally
# named "bash\r" and the container crash-loops with:
#     /usr/bin/env: 'bash\r': No such file or directory
# .gitattributes pins these to LF for fresh clones; this line additionally makes an ALREADY-CRLF working
# tree build correctly, so a Windows user doesn't have to re-clone. Cheap and idempotent on Linux.
RUN sed -i 's/\r$//' /app/docker-entrypoint.sh && chmod +x /app/docker-entrypoint.sh \
 && find /app -maxdepth 2 -name '*.sh' -exec sed -i 's/\r$//' {} +

# Built-in TURN relay binary (compiled in the turnbuild stage). The app supervises it (turn_service.py)
# when POSTERCHANAI_TURN=1 + a public IP is set; it's a no-op otherwise.
COPY --from=turnbuild /build/pion-turn /app/turnserver/pion-turn

# Built-in MediaMTX media server (downloaded in the streamdl stage). The app supervises it
# (stream_service.py) when POSTERCHANAI_STREAM=1; it's a no-op otherwise.
COPY --from=streamdl /mediamtx /app/streamserver/mediamtx

# Runtime data lives on a volume: uploads, downloaded models, HF caches, and /app/data
# (the keyfile). Durable app/relay state is in PostgreSQL (the compose `postgres` service).
RUN mkdir -p /var/lib/posterchanai/models /var/lib/posterchanai/torrents \
             /var/lib/posterchanai/tor /var/lib/posterchanai/tor2 /var/lib/posterchanai/hf \
             /var/lib/posterchanai/git_repos /app/data
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
    DOWNLOAD_U2NET_MODEL=1 \
    U2NET_HOME=/var/lib/posterchanai/u2net \
    U2NET_MODEL_URL=https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx \
    POSTERCHANAI_LLM_MODEL_PATH=/var/lib/posterchanai/models/Qwen3.5-9B-abliterated-Q4_K_M.gguf \
    POSTERCHANAI_MODEL_URL=https://huggingface.co/lukey03/Qwen3.5-9B-abliterated-GGUF/resolve/main/Qwen3.5-9B-abliterated-Q4_K_M.gguf \
    POSTERCHANAI_LLM_TOOLS_MODEL= \
    POSTERCHANAI_IMAGE_MODEL_PATH=Lykon/dreamshaper-8 \
    POSTERCHANAI_IMAGE_MODEL_TYPE=sd15 \
    POSTERCHANAI_TOR_ENABLED=true \
    POSTERCHANAI_PROXY_ENABLED=true \
    POSTERCHANAI_BT_ENABLED=false
# Built-in Tor + the :8118 HTTP proxy are ON by default (PosterChanAI starts/manages the Tor daemon
# itself — nothing auto-starts a Tor process at boot otherwise). This matches the regular install's
# defaults and means outbound relay/social traffic is proxied out of the box; without it the proxy
# never listened on :8118 and every upstream connect hit ECONNREFUSED before falling back to direct.
# Torrenting stays OFF (opt in with -e POSTERCHANAI_BT_ENABLED=true). Disable the proxy/Tor with
# -e POSTERCHANAI_TOR_ENABLED=false -e POSTERCHANAI_PROXY_ENABLED=false, or toggle in Admin → Services
# (seeded only on first run, so this never overrides an existing choice).

EXPOSE 3051
# Built-in Nostr WoT relay (NIP-01). ON by default — it is the app's datastore AND the relay the web
# client connects to, so the container always binds it to 0.0.0.0 (loopback-only would leave this
# published port answering nothing). POSTERCHANAI_NOSTR_RELAY=1 additionally forces it on for an
# existing install that had turned it off. See docs/RELAY.md.
EXPOSE 3052

# Built-in TURN/STUN relay for voice/video calls. OFF unless POSTERCHANAI_TURN=1 + a public IP is set.
# For calls behind NAT this port (and the relay UDP range) must be reachable — publish them and forward
# them on your router; a direct grey-clouded turn.<domain> record is recommended (it can't ride CF Tunnel).
EXPOSE 3478/udp
EXPOSE 3478/tcp
# TURN relayed-media UDP range (PC_TURN_MIN_PORT..MAX_PORT; must match docker-compose's published range).
EXPOSE 49160-49200/udp

# OBS streaming (MediaMTX). OFF unless POSTERCHANAI_STREAM=1. 1935 = RTMP ingest (from OBS); 8888 = HLS
# output (the app reverse-proxies it unless stream_hls_base points at a direct subdomain).
EXPOSE 1935/tcp
EXPOSE 8888/tcp
# WebRTC/WHIP ingest (phone go-live): 8889 = WHIP signaling, 8189/udp = WebRTC media (forward for remote phones).
EXPOSE 8889/tcp
EXPOSE 8189/udp

# TCP health check on the configured port (the UI redirects to /login, so a plain
# socket connect is a cleaner liveness probe than an HTTP status check).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python3 -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('POSTERCHANAI_PORT','3051'))), 5)" || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "run.py"]
