#!/usr/bin/env bash
# PosterChanAI container entrypoint.
#   - prepares the runtime data dirs (mounted volume)
#   - keeps the sqlite DB on the data volume so it survives container recreation
#   - optionally sources Intel oneAPI (only needed for the SYCL llama.cpp LLM path)
#   - then execs the CMD (python run.py)
set -e

DATA_HOME="${POSTERCHANAI_DATA:-/var/lib/posterchanai}"
mkdir -p "$DATA_HOME"/{models,torrents,tor,hf,miopen,assets} /app/data/chromadb

# Persist the sqlite DB on the data volume: the app opens ./posterchanai.db in
# /app, so point that path at the volume (symlink target is created by sqlite on
# first run). Move an existing in-image DB onto the volume once.
if [ ! -L /app/posterchanai.db ]; then
    [ -f /app/posterchanai.db ] && mv -n /app/posterchanai.db "$DATA_HOME/posterchanai.db"
    ln -sf "$DATA_HOME/posterchanai.db" /app/posterchanai.db
fi

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

# Intel only: the SYCL-built llama.cpp needs the oneAPI runtime libraries on the
# loader path. It is OFF by default because torch-XPU (image generation) bundles
# its own oneAPI and the two can clash in one process — enable it with
# SOURCE_ONEAPI=1 when you want GPU LLM rather than image gen on Intel Arc.
if [ "${SOURCE_ONEAPI:-0}" = "1" ] && [ -f /opt/intel/oneapi/setvars.sh ]; then
    echo "[entrypoint] sourcing Intel oneAPI for SYCL llama.cpp"
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

exec "$@"
