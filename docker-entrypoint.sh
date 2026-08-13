#!/usr/bin/env bash
# PosterChanAI container entrypoint.
#   - prepares the runtime data dirs (mounted volume)
#   - optionally sources Intel oneAPI (only needed for the SYCL llama.cpp LLM path)
#   - then execs the CMD (python run.py)
# The app + relay use PostgreSQL (the `postgres` service in docker-compose), not SQLite —
# durable state lives in PG; the data volume holds uploads/models/HF caches + /app/data (keyfile).
set -e

DATA_HOME="${POSTERCHANAI_DATA:-/var/lib/posterchanai}"
mkdir -p "$DATA_HOME"/{models,torrents,tor,tor2,hf,miopen,assets} /app/data

# --- Timezone, THEN clock sync, BEFORE the app/relay start ---------------------------------------
# The Nostr relay's queries are time-windowed (backfill `since = now - 48h`, created_at sanity), so
# a wrong system clock silently breaks federation: the WoT still builds (kind-3 has no time filter)
# but the timeline stays EMPTY (the post window is in the future).

# 1) Timezone first, so logs/`date` read in the configured zone (default UTC). TZ is the env knob;
#    link /etc/localtime when tzdata is present so libc-based code agrees with it.
export TZ="${TZ:-UTC}"
if [ -e "/usr/share/zoneinfo/$TZ" ]; then
    ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime 2>/dev/null || true
    echo "$TZ" > /etc/timezone 2>/dev/null || true
    echo "[entrypoint] timezone set to $TZ"
else
    echo "[entrypoint] timezone $TZ not found in tzdata — leaving as-is"
fi

# 2) Then update the clock from NTP. Best-effort: if off by > 60s, set it via `date -s`. This needs
#    CAP_SYS_TIME — the compose file grants it (cap_add: SYS_TIME); a bare `docker run` needs
#    --cap-add SYS_TIME, else the clock is the HOST's job and we warn. Skip with POSTERCHANAI_NTP_SYNC=0.
if [ "${POSTERCHANAI_NTP_SYNC:-1}" = "1" ]; then
    python3 - <<'PYEOF' || true
import socket, struct, time, subprocess
def ntp_time():
    for host in ("pool.ntp.org", "time.google.com", "time.cloudflare.com"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(5)
            s.sendto(b"\x1b" + 47 * b"\0", (host, 123))
            data, _ = s.recvfrom(48); s.close()
            return struct.unpack("!12I", data)[10] - 2208988800  # NTP epoch -> Unix
        except Exception:
            continue
    return None
real = ntp_time()
if real is None:
    print("[entrypoint] clock: could not reach NTP (DNS/network?) — skipping check", flush=True)
else:
    skew = real - time.time()
    if abs(skew) <= 60:
        print(f"[entrypoint] clock OK (NTP skew {skew:+.0f}s)", flush=True)
    else:
        print(f"[entrypoint] clock is off by {skew:+.0f}s "
              f"(system={time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}, "
              f"real={time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(real))})", flush=True)
        if subprocess.run(["date", "-s", "@%d" % int(real)],
                          capture_output=True).returncode == 0:
            print(f"[entrypoint] clock corrected via NTP to "
                  f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(real))}", flush=True)
        else:
            print("[entrypoint] WARNING: could NOT set the clock (need --cap-add SYS_TIME, or fix "
                  "the HOST clock / enable NTP). The Nostr relay will show NO posts until the clock "
                  "is correct.", flush=True)
PYEOF
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

# ---------------------------------------------------------------------------------------------
# THE BUNDLED SearXNG'S SECRET
#
# The settings file is baked into the image (docker/searxng/settings.yml → /etc/searxng), so its
# `secret_key` is committed — and a committed key is the SAME key on every deployment. It only signs
# this instance's preference cookies, and the mount refuses anything that arrived through a reverse
# proxy, so it is not a hole; it is just free to close. SEARXNG_SECRET if the operator set one, a
# random value otherwise, and only while the placeholder is still there — a rewrite on every start
# would invalidate every preference cookie on every restart.
_sx_settings="${SEARXNG_SETTINGS_PATH:-/etc/searxng/settings.yml}"
if [ -w "$_sx_settings" ] && grep -q 'secret_key: "ultrasecretkey"' "$_sx_settings" 2>/dev/null; then
    _sx_secret="${SEARXNG_SECRET:-$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
    if sed -i "s|secret_key: \"ultrasecretkey\"|secret_key: \"${_sx_secret}\"|" "$_sx_settings" 2>/dev/null; then
        echo "[entrypoint] SearXNG: generated this node's secret_key"
    else
        echo "[entrypoint] SearXNG: could not write $_sx_settings — keeping the placeholder secret"
    fi
    unset _sx_secret
fi
unset _sx_settings

# ---------------------------------------------------------------------------------------------
# DOES THIS NODE WANT MODEL WEIGHTS AT ALL?
#
# The DOWNLOAD_* defaults below are ENV in the shared final image stage, so they are 1 in EVERY
# image — including the nostr-only one, whose whole pitch is "a relay + client + Blossom, no AI
# stack". That image installs requirements-nostr.txt: no llama-cpp, no onnxruntime, no rembg. So a
# plain `docker compose --profile nostr up -d` started pulling ~5.9 GB of weights (a 5.6 GB GGUF, a
# 94 MB depth model, a 176 MB u2net) that NOTHING in the container can load — in the background,
# onto the data volume, on the deployment least likely to have the disk or the bandwidth for it.
#
# Two independent signals, because they answer different questions and either one alone leaves a
# hole:
#   PC_ACCEL=nostr             — a BUILD fact baked into the image: it has no AI libraries. True
#                                even for `docker run` without the compose environment.
#   POSTERCHANAI_NOSTR_ONLY=1  — the OPERATOR asking for a Nostr-only node. An AI-capable image can
#                                be run this way, and the AI surfaces are hidden, so pre-fetching a
#                                chat model nothing exposes is still 5.6 GB of nothing.
# Said out loud rather than skipped quietly: "no model appeared" and "the download failed" look
# identical in a log that says neither.
PC_WANT_MODELS=1
if [ "${PC_ACCEL:-}" = "nostr" ] || \
   [ "$(echo "${POSTERCHANAI_NOSTR_ONLY:-0}" | tr 'A-Z' 'a-z')" = "1" ] || \
   [ "$(echo "${POSTERCHANAI_NOSTR_ONLY:-0}" | tr 'A-Z' 'a-z')" = "true" ] || \
   [ "$(echo "${POSTERCHANAI_NOSTR_ONLY:-0}" | tr 'A-Z' 'a-z')" = "yes" ] || \
   [ "$(echo "${POSTERCHANAI_NOSTR_ONLY:-0}" | tr 'A-Z' 'a-z')" = "on" ]; then
    PC_WANT_MODELS=0
    echo "[entrypoint] Nostr-only node — skipping every model pre-fetch (chat / depth / u2net):" \
         "this build has no AI stack to load them with. Nothing is downloaded."
fi

# Chat model: NOT auto-downloaded (saves bandwidth). The admin pulls it on demand from
# Admin → LLM → "Download chat model" (shows progress + ✓/✗). Opt back into the
# turnkey background pull with DOWNLOAD_MODEL=1 (needs POSTERCHANAI_MODEL_URL + _LLM_MODEL_PATH).
if [ "$PC_WANT_MODELS" = "1" ] && [ "${DOWNLOAD_MODEL:-0}" = "1" ] && [ -n "${POSTERCHANAI_MODEL_URL:-}" ] && \
   [ -n "${POSTERCHANAI_LLM_MODEL_PATH:-}" ] && [ ! -f "$POSTERCHANAI_LLM_MODEL_PATH" ]; then
    (
        tmp="${POSTERCHANAI_LLM_MODEL_PATH}.part"
        echo "[entrypoint] downloading recommended model in background -> $POSTERCHANAI_LLM_MODEL_PATH"
        if curl -fL --retry 5 --retry-delay 10 -C - -o "$tmp" "$POSTERCHANAI_MODEL_URL"; then
            mv -f "$tmp" "$POSTERCHANAI_LLM_MODEL_PATH"
            echo "[entrypoint] model download complete: $POSTERCHANAI_LLM_MODEL_PATH"
        else
            echo "[entrypoint] WARNING: model download failed; set a model in Admin → LLM or retry."
        fi
    ) &
fi

# Depth model for the `alive` 3D-parallax effect (~94 MB, gitignored so not baked into
# the image). Fetched on first run into the data volume, in the BACKGROUND so startup
# isn't blocked; the effect lights up once it lands. Skip with DOWNLOAD_DEPTH_MODEL=0.
if [ "$PC_WANT_MODELS" = "1" ] && [ "${DOWNLOAD_DEPTH_MODEL:-0}" = "1" ] && [ -n "${DEPTH_MODEL_URL:-}" ] && \
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
if [ "$PC_WANT_MODELS" = "1" ] && [ "${DOWNLOAD_U2NET_MODEL:-0}" = "1" ] && [ -n "${U2NET_MODEL_URL:-}" ] && \
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

# Provision the relay's instance (operator) key + seed the Web of Trust with it, and print the
# instance npub (idempotent). The app also mints this lazily on first start, but doing it here gives
# parity with install.sh and surfaces the npub in the container logs. Best-effort — never block
# startup (postgres is already healthy via compose depends_on by the time the entrypoint runs).
if [ -f /app/scripts/init_instance_key.py ]; then
    python /app/scripts/init_instance_key.py || echo "[entrypoint] instance key init deferred to app start"
fi

exec "$@"
