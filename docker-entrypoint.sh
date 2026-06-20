#!/usr/bin/env bash
# PosterChanAI container entrypoint.
#   - prepares the runtime data dirs (mounted volume)
#   - optionally sources Intel oneAPI (only needed for the SYCL llama.cpp LLM path)
#   - then execs the CMD (python run.py)
# The app + relay use PostgreSQL (the `postgres` service in docker-compose), not SQLite —
# durable state lives in PG; the data volume holds uploads/models/HF caches + /app/data (keyfile).
set -e

DATA_HOME="${POSTERCHANAI_DATA:-/var/lib/posterchanai}"
mkdir -p "$DATA_HOME"/{models,torrents,tor,hf,miopen,assets} /app/data

# AMD only: consumer RDNA cards aren't in ROCm's official support list, so torch /
# HIP need HSA_OVERRIDE_GFX_VERSION pointed at the nearest supported arch. Auto-set
# it from the detected GPU (unless the user already provided one) so the AMD image
# works out of the box. rocminfo only exists in the rocm build and needs /dev/kfd.
if [ -z "${HSA_OVERRIDE_GFX_VERSION:-}" ] && command -v rocminfo >/dev/null 2>&1; then
    gfx="$(rocminfo 2>/dev/null | grep -m1 -oE 'gfx[0-9a-f]+' || true)"
    case "$gfx" in
        gfx1030|gfx1100|gfx1101|gfx1200|gfx1201) : ;;            # natively supported
        gfx103*) export HSA_OVERRIDE_GFX_VERSION=10.3.0 ;;       # RDNA2 consumer (6600–6750)
        gfx101*) export HSA_OVERRIDE_GFX_VERSION=10.1.0 ;;       # RDNA1 (5000 series)
        gfx110[2-9]|gfx111*) export HSA_OVERRIDE_GFX_VERSION=11.0.0 ;;  # RDNA3 consumer (7600 …)
    esac
    [ -n "${HSA_OVERRIDE_GFX_VERSION:-}" ] && \
        echo "[entrypoint] AMD $gfx -> HSA_OVERRIDE_GFX_VERSION=$HSA_OVERRIDE_GFX_VERSION"
fi

# Intel (unified stack): chat (llama.cpp SYCL) + image (diffusers torch-XPU) run from ONE
# venv/service. Both use torch-XPU's BUNDLED oneAPI runtime (/opt/venv/lib) — do NOT source
# the system oneAPI, mixing the two triggers the LIBUR_LOADER symbol mismatch. Two musts:
#   * ONEAPI_DEVICE_SELECTOR=level_zero:gpu — else llama.cpp SYCL silently picks the CPU
#     device (symptom: ~2 tok/s instead of ~19).
#   * image_subprocess_mode on — one image subprocess per gen releases VRAM on the shared GPU.
# All overridable; matches the proven bare-metal config (run-intel.sh).
if [ "${PC_ACCEL:-}" = "intel" ]; then
    export LD_LIBRARY_PATH="/opt/venv/lib:/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"
    export ONEAPI_DEVICE_SELECTOR="${ONEAPI_DEVICE_SELECTOR:-level_zero:gpu}"
    export ZES_ENABLE_SYSMAN=1
    export SYCL_CACHE_PERSISTENT=1
    export POSTERCHANAI_IMAGE_SUBPROCESS_MODE="${POSTERCHANAI_IMAGE_SUBPROCESS_MODE:-true}"
    echo "[entrypoint] Intel unified stack: torch-XPU runtime, ONEAPI_DEVICE_SELECTOR=$ONEAPI_DEVICE_SELECTOR, subprocess image mode"
fi
# (Legacy escape hatch) source system oneAPI only if explicitly asked — not needed by the
# unified stack and known to clash with the bundled runtime.
if [ "${SOURCE_ONEAPI:-0}" = "1" ] && [ -f /opt/intel/oneapi/setvars.sh ]; then
    echo "[entrypoint] sourcing Intel oneAPI for SYCL llama.cpp (SOURCE_ONEAPI=1)"
    # shellcheck disable=SC1091
    source /opt/intel/oneapi/setvars.sh >/dev/null 2>&1 || true
fi

# Turnkey model: download the recommended GGUF on first run so `native` chat works
# out of the box. Done in the BACKGROUND (it's ~5.6 GB) so the web UI is available
# immediately; chat starts working once the file lands (progress in the log). Skip
# with DOWNLOAD_MODEL=0, or if the user already placed/Configured a model.
if [ "${DOWNLOAD_MODEL:-1}" = "1" ] && [ -n "${POSTERCHANAI_MODEL_URL:-}" ] && \
   [ -n "${POSTERCHANAI_LLM_MODEL_PATH:-}" ] && [ ! -f "$POSTERCHANAI_LLM_MODEL_PATH" ]; then
    (
        tmp="${POSTERCHANAI_LLM_MODEL_PATH}.part"
        echo "[entrypoint] downloading recommended model in background -> $POSTERCHANAI_LLM_MODEL_PATH"
        if curl -fL --retry 5 --retry-delay 10 -C - -o "$tmp" "$POSTERCHANAI_MODEL_URL"; then
            mv -f "$tmp" "$POSTERCHANAI_LLM_MODEL_PATH"
            echo "[entrypoint] model download complete: $POSTERCHANAI_LLM_MODEL_PATH"
        else
            echo "[entrypoint] WARNING: model download failed; set a model in Admin → Settings or retry."
        fi
    ) &
fi

# Depth model for the `alive` 3D-parallax effect (~94 MB, gitignored so not baked into
# the image). Fetched on first run into the data volume, in the BACKGROUND so startup
# isn't blocked; the effect lights up once it lands. Skip with DOWNLOAD_DEPTH_MODEL=0.
if [ "${DOWNLOAD_DEPTH_MODEL:-1}" = "1" ] && [ -n "${DEPTH_MODEL_URL:-}" ] && \
   [ -n "${DEPTH_MODEL_PATH:-}" ] && [ ! -f "$DEPTH_MODEL_PATH" ]; then
    (
        tmp="${DEPTH_MODEL_PATH}.part"
        echo "[entrypoint] downloading depth model (alive effect) -> $DEPTH_MODEL_PATH"
        if curl -fL --retry 5 --retry-delay 10 -C - -o "$tmp" "$DEPTH_MODEL_URL"; then
            mv -f "$tmp" "$DEPTH_MODEL_PATH"
            echo "[entrypoint] depth model ready: $DEPTH_MODEL_PATH"
        else
            echo "[entrypoint] WARNING: depth model download failed; the 'alive' command stays disabled until it's present."
        fi
    ) &
fi

# u2net ONNX for the `removebackground` command (rembg). ~176MB; fetched on first run into the
# data volume (rembg's U2NET_HOME) in the BACKGROUND. rembg would otherwise fetch it lazily on
# the first removebackground; pre-fetching means the first call doesn't stall. Skip with
# DOWNLOAD_U2NET_MODEL=0.
if [ "${DOWNLOAD_U2NET_MODEL:-1}" = "1" ] && [ -n "${U2NET_MODEL_URL:-}" ] && \
   [ -n "${U2NET_HOME:-}" ] && [ ! -f "$U2NET_HOME/u2net.onnx" ]; then
    (
        mkdir -p "$U2NET_HOME"
        tmp="$U2NET_HOME/u2net.onnx.part"
        echo "[entrypoint] downloading background-removal model (removebackground) -> $U2NET_HOME/u2net.onnx"
        if curl -fL --retry 5 --retry-delay 10 -C - -o "$tmp" "$U2NET_MODEL_URL"; then
            mv -f "$tmp" "$U2NET_HOME/u2net.onnx"
            echo "[entrypoint] background-removal model ready: $U2NET_HOME/u2net.onnx"
        else
            echo "[entrypoint] WARNING: u2net download failed; removebackground fetches it on first use instead."
        fi
    ) &
fi

exec "$@"
