"""Supervise the built-in MediaMTX media server (streamserver/mediamtx) as a subprocess.

This powers OBS streaming: a self-hosted RTMP ingest (OBS pushes here) that MediaMTX remuxes to HLS for
playback in the web client's NIP-53 Streams tab. It mirrors turn_service.py / bot_manager_service: a
monitor thread reconciles the desired state every few seconds, (re)spawning the binary with a generated
config from the `stream_*` settings and restarting it on crash with an hourly cap.

Design (see app/routers/streams.py):
- OBS publishes to `rtmp://<host>:<rtmp_port>/<token>?key=<api_key>`. MediaMTX calls back to the app's
  /api/streams/auth hook to validate the API key on publish; playback (HLS reads) is public.
- The API key is NEVER in the public HLS URL — only the random <token> is (it rides the public kind-30311).
- Turnkey: the binary is downloaded by install.sh --stream / the Docker build; absent → silent no-op.
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from app.services import settings_store

logger = logging.getLogger(__name__)

# repo_root/streamserver/mediamtx  (app/services/stream_service.py -> parents[2] == repo root)
_STREAM_DIR = Path(__file__).resolve().parents[2] / "streamserver"
_STREAM_BIN = _STREAM_DIR / "mediamtx"
_STREAM_CFG = _STREAM_DIR / "mediamtx.gen.yml"   # generated each spawn (gitignored)
_CLAMP_SCRIPT = _STREAM_DIR / "clamp.sh"         # generated each spawn (gitignored); see _write_clamp_script

# Suffix of the transcoded ("clamped") MediaMTX path that viewers actually watch when the bitrate clamp is
# on: source `<token>` in, `<token>_clamped` out. Shared with app/routers/streams.py, which resolves which of
# the two to serve and authorizes the clamp's loopback publish — keep it in ONE place or the two disagree
# and every viewer silently gets the unclamped source.
CLAMP_SUFFIX = "_clamped"

# ---- per-streamer quality tiers -------------------------------------------------------------------
# A streamer can ask for LESS than the node's ceiling — "my connection is bad today", "I'm on data". Each
# tier is (height, fps, video-bitrate) and is capped against the admin settings when the clamp script is
# generated, so a tier can only ever LOWER what the clamp already does: this is not a way to ask the node
# for more bandwidth than the operator allowed.
# Chosen per stream (not per node) and read at stream START from a file, so switching tiers needs no
# MediaMTX respawn — a config change respawns it and drops every live stream, which would be an absurd
# price for one streamer changing their own quality.
QUALITY_TIERS = {
    "720": (720, 30, "1500k"),
    "480": (480, 30, "900k"),
    "360": (360, 24, "500k"),
}


def quality_dir() -> str:
    """Directory holding one file per token: the streamer's chosen tier. Lives beside the generated
    clamp script, is created on demand, and is read by clamp.sh — so it must be a plain path with no
    shell metacharacters (it is interpolated into the generated script)."""
    d = _STREAM_DIR / "quality"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("[stream] could not create %s: %s", d, e)
    return str(d)


def _quality_token_ok(token: str) -> bool:
    """A token is a path segment in MediaMTX AND a filename here — allow only what both accept."""
    return bool(token) and len(token) <= 128 and all(c.isalnum() or c in "-_" for c in token)


def set_quality(token: str, tier: str) -> str:
    """Record a streamer's tier choice. `tier` outside QUALITY_TIERS means AUTO (the node default), which
    is stored as a removal so there is no stale file to reason about. Returns the tier now in force."""
    if not _quality_token_ok(token):
        raise ValueError("bad stream token")
    path = os.path.join(quality_dir(), token)
    tier = (tier or "").strip()
    if tier not in QUALITY_TIERS:
        try:
            os.unlink(path)
        except OSError:
            pass
        return "auto"
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:      # write+rename: clamp.sh may read this the instant a stream starts
        fh.write(tier)
    os.replace(tmp, path)
    return tier


def get_quality(token: str) -> str:
    """The tier in force for this token, or "auto"."""
    if not _quality_token_ok(token):
        return "auto"
    try:
        with open(os.path.join(quality_dir(), token)) as fh:
            v = fh.read().strip()
        return v if v in QUALITY_TIERS else "auto"
    except OSError:
        return "auto"

_RECONCILE_INTERVAL = 5
_MAX_RESTARTS_PER_HOUR = 12
_RESTART_WINDOW = 3600

_lock = threading.RLock()
_proc: Optional[subprocess.Popen] = None
_monitor_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_restart_count = 0
_restart_window_start = 0.0
_gaveup_logged = False
_spawn_sig: Optional[str] = None

# Settings whose change requires regenerating the config + respawning mediamtx.
_SIG_KEYS = ("stream_rtmp_port", "stream_hls_port", "stream_srt_port",
             "stream_webrtc_port", "stream_webrtc_udp_port", "stream_domain", "turn_public_ip",
             "stream_auth_secret", "stream_api_port",
             # Recording toggle/dir must be here or MediaMTX is never respawned with `record: yes`, so
             # flipping stream_record_enabled on would silently record nothing. The trade-off: toggling it
             # respawns MediaMTX (like any config key), which briefly drops a currently-live stream — an
             # admin set-and-forget setting, so that's preferred over silently not recording.
             "stream_record_enabled", "stream_record_dir",
             # Same reasoning as the recording keys: the clamp is a `runOnReady` command baked into the
             # generated config + script, so MediaMTX must be respawned for a change to take effect.
             "stream_clamp_enabled", "stream_clamp_height", "stream_clamp_fps", "stream_clamp_bitrate",
             "stream_clamp_audio_bitrate", "stream_clamp_encoder", "stream_rtsp_port")


def _ensure_hook_secret(cfg: dict) -> str:
    """Secret the app injects into MediaMTX's auth-hook URL so /api/streams/auth can verify the call came
    from our MediaMTX (not a forged public request) — robust regardless of proxy/loopback IP quirks."""
    sec = (cfg.get("stream_auth_secret", "") or "").strip()
    if sec:
        return sec
    sec = secrets.token_hex(24)
    try:
        settings_store.put("stream_auth_secret", sec)
        logger.info("[stream] generated a stream auth-hook secret on first start")
    except Exception as e:  # pragma: no cover
        logger.warning("[stream] could not persist auth secret: %s", e)
    return sec


def _app_port() -> str:
    return (os.environ.get("POSTERCHANAI_PORT", "") or "3051").strip()


def _cfg() -> dict:
    return settings_store.all_settings()


def recording_dir() -> str:
    """The (sanitized) temp dir MediaMTX records into, one subdir per stream token. Single source of
    truth shared with stream_vod_service so both agree on where recordings live. Just a path — mount it
    as tmpfs/point it at /dev/shm for RAM-backed recording; the app doesn't care. Sanitized because it's
    interpolated into the MediaMTX YAML (recordPath) — no quotes/space injection."""
    import re
    d = (settings_store.get("stream_record_dir", "") or "/tmp/posterchanai-streams").strip()
    d = re.sub(r"[^A-Za-z0-9._/\-]", "", d).rstrip("/")
    return d or "/tmp/posterchanai-streams"


def _enabled(cfg: dict) -> bool:
    return (cfg.get("stream_enabled", "false") or "").strip().lower() == "true"


# ---------------------------------------------------------------- Bitrate clamp (live transcode)

def clamp_enabled(cfg: Optional[dict] = None) -> bool:
    """Is the live bitrate clamp on? Default ON — a streamer's OBS settings otherwise dictate what every
    viewer downloads (MediaMTX is a pure remux), which is the instance's bandwidth bill, not theirs.

    Reads the single key rather than `_cfg()` when no dict is supplied: the HLS proxy asks this once per
    segment per viewer, and `all_settings()` copies the whole settings dict on every call.
    """
    v = cfg.get("stream_clamp_enabled", "true") if cfg is not None \
        else settings_store.get("stream_clamp_enabled", "true")
    return (v or "").strip().lower() == "true"


def clamp_secret(cfg: Optional[dict] = None) -> str:
    """Secret proving an RTSP publish is our own clamp (see /api/streams/auth).

    Derived from the auth-hook secret rather than stored separately: one less setting to persist/sync, it
    rotates with its parent, and the parent itself never appears on a command line.

    Why a secret at all, when the clamp only ever connects over loopback: MediaMTX's reported publisher `ip`
    is NOT dependable — measured against v1.19.2, a connection to a 127.0.0.1-bound RTSP listener was
    reported as the machine's LAN address, so an IP check would deny every clamp and silently serve viewers
    the unclamped source. The URL query IS forwarded to the hook verbatim, so that's what we key on.

    Limitation, stated plainly: the secret is in the ffmpeg argv, so a local user who can read `ps` could
    publish to a clamped path and replace what viewers see. That user can already read the generated config
    and the settings store on this machine, so it grants nothing they didn't have; it is NOT a defence
    against anything remote, which is what this gate is for.
    """
    c = cfg if cfg is not None else _cfg()
    parent = (c.get("stream_auth_secret", "") or "").strip()
    if not parent:
        return ""
    import hashlib
    import hmac as _hmac
    return _hmac.new(parent.encode(), b"pcai-stream-clamp", hashlib.sha256).hexdigest()[:32]


def _rtsp_port(cfg: dict) -> str:
    p = (cfg.get("stream_rtsp_port", "") or "8554").strip()
    return p if p.isdigit() else "8554"


def _clamp_params(cfg: dict) -> dict:
    """Validated clamp knobs. Every value is interpolated into a generated shell script, so each one is
    checked against a strict shape and falls back to its default rather than being escaped — an admin
    setting is not a trusted string (settings sync from the relay across nodes)."""
    import re

    def _int(key: str, default: str, lo: int, hi: int) -> str:
        v = (cfg.get(key, "") or "").strip()
        return v if v.isdigit() and lo <= int(v) <= hi else default

    def _rate(key: str, default: str) -> str:
        v = (cfg.get(key, "") or "").strip()
        return v if re.fullmatch(r"\d{1,7}[kKmM]?", v) else default

    enc = (cfg.get("stream_clamp_encoder", "") or "").strip()
    return {
        "height": _int("stream_clamp_height", "720", 144, 2160),
        "fps": _int("stream_clamp_fps", "30", 5, 120),
        "vbitrate": _rate("stream_clamp_bitrate", "1500k"),
        "abitrate": _rate("stream_clamp_audio_bitrate", "128k"),
        "encoder": enc if re.fullmatch(r"[A-Za-z0-9_]{1,32}", enc) else "",
    }


def _rate_kbps(rate: str, default: int = 1500) -> int:
    """"1500k" -> 1500, "3M" -> 3000, "800" -> 800 (bare digits are already kbit/s here, matching how the
    admin field reads). The clamp does arithmetic on the ceiling at runtime, so it needs a number."""
    import re
    m = re.fullmatch(r"(\d+)([kKmM]?)", (rate or "").strip())
    if not m:
        return default
    n = int(m.group(1))
    return n * 1000 if m.group(2) in ("m", "M") else n


def _clamp_encoder(cfg: dict) -> str:
    """The H.264 encoder the clamp runs. Reuses the same NVENC → VAAPI → libx264 autodetect as offline video
    work so a node's hardware is probed in exactly one place. Picked at config-generation time because the
    ffmpeg command is baked into the script; the script itself still falls back to libx264 at runtime if the
    hardware encoder turns out not to work (a probe can succeed where the actual encode fails)."""
    forced = _clamp_params(cfg)["encoder"]
    if forced:
        return forced
    try:
        from app.services.media_service import _video_encoder_candidates, resolve_ffmpeg
        return _video_encoder_candidates(resolve_ffmpeg())[0]
    except Exception as e:
        logger.warning("[stream] clamp encoder autodetect failed (%s) — using libx264", e)
        return "libx264"


def _clamp_video_args(encoder: str, p: dict) -> tuple:
    """ffmpeg video args for one encoder as (pre_input, post_input) shell fragments.

    Split in two because `-vaapi_device` is a GLOBAL option: ffmpeg silently ignores it after `-i`, then
    fails to open the VAAPI encoder with no useful error. Everything else is a per-output option.

    The scale filter caps the SHORT side at H and never upscales. Capping the short side (rather than the
    height) is what makes "720p" mean 720p for BOTH orientations: a portrait 1080x1920 phone stream becomes
    720x1280, where a plain height cap would squeeze it to 406x720 — much softer than asked for, and saving
    nothing, because the bitrate ceiling is what actually bounds bandwidth. Downscale-only matters too: a
    480p source stays 480p, since upscaling it would cost MORE than not clamping at all.
    Verified against lavfi at 1920x1080 / 1080x1920 / 1280x720 / 854x480 / 640x480 / 3840x2160.
    VAAPI scales on the CPU and then uploads — scale_vaapi has no expression support at all.

    Both fragments are inlined VERBATIM into the generated script (never passed through a shell variable),
    so the quotes below are parsed by the shell as written. Expanding them from a variable would not
    re-parse the quotes and ffmpeg would receive literal `"` characters inside the filter string.
    """
    gop = str(int(p["fps"]) * 2)      # keyframe every 2s == hlsSegmentDuration, so segments cut cleanly
    # gte(iw,ih) picks the orientation; -2 keeps the aspect ratio (rounded to an even, encodable size).
    scale = ("scale=w='if(gte(iw,ih),-2,min({h},iw))':h='if(gte(iw,ih),min({h},ih),-2)'"
             .format(h=p["height"]))
    # The bitrate is a CEILING, never a target — and getting that wrong is silent and backwards. A plain
    # `-b:v X` under ffmpeg's DEFAULT rate control pads every stream up to X: measured on the Arc, a
    # 125 kbps phone source came out at 1441 kbps, an 11.5x INFLATION, hitting hardest exactly the weak
    # connections this feature exists to protect. Explicit VBR/CRF spends only what the picture needs:
    #     125 kbps in -> 126 kbps out   (was 1441)
    #    2.5 Mbps in -> 277 kbps out    (was 1441)
    #    9.5 Mbps in -> 1485 kbps out   (ceiling still holds)
    # Each encoder spells capped-quality differently; they are NOT interchangeable, and the wrong spelling
    # silently reverts to padding rather than erroring. bufsize is 2x the ceiling — a 1x buffer is
    # effectively CBR and reintroduces the padding.
    # $VMAX/$VBUF are computed by the generated script at stream start (see _write_clamp_script): the
    # effective ceiling is min(what the source actually sends, the configured ceiling). Rate control alone
    # cannot prevent a weak source being inflated — re-encoding low-bitrate video is expensive no matter
    # the mode, because the compression artefacts are themselves detail the encoder must reproduce. The
    # only thing that works is refusing to spend more than the source did.
    v, buf = "${VMAX}k", "${VBUF}k"
    # Every chain ends in an explicit 4:2:0 `format` filter. Without it the encoder inherits the SOURCE
    # pixel format, and a 4:4:4 input (some capture cards, some screen-share paths) makes libx264 reject
    # `-profile:v main` outright — the clamp would fail on exactly the streams the CPU fallback exists for.
    # 4:2:0 is also the only chroma format browsers reliably decode, so this is what viewers need anyway.
    if encoder == "h264_nvenc":
        # NVENC capped-quality: -cq with `-b:v 0` (a non-zero -b:v would re-assert a target).
        return ("", f"-vf \"fps={p['fps']},{scale},format=nv12\" -c:v h264_nvenc -preset p4 "
                    f"-rc vbr -cq 24 -b:v 0 -maxrate {v} -bufsize {buf} -g {gop}")
    if encoder == "h264_vaapi":
        # VAAPI: -rc_mode VBR. Its ICQ/QVBR modes either aren't supported by the iHD driver or ignore
        # maxrate entirely (both measured), so VBR is the only mode here that caps without padding.
        return ("-vaapi_device \"$VAAPI_DEVICE\"",
                f"-vf \"fps={p['fps']},{scale},format=nv12,hwupload\" -c:v h264_vaapi "
                f"-rc_mode VBR -b:v {v} -maxrate {v} -bufsize {buf} -g {gop}")
    return ("", f"-vf \"fps={p['fps']},{scale},format=yuv420p\" -c:v libx264 -preset veryfast "
                f"-profile:v main -crf 23 -maxrate {v} -bufsize {buf} -g {gop} -sc_threshold 0")


def _write_clamp_script(cfg: dict) -> None:
    """Generate streamserver/clamp.sh — the live transcode MediaMTX runs per stream via `runOnReady`.

    MediaMTX supervises this process itself (starts it when a source goes live, kills it when the source
    stops), so there is no Python supervisor to write, crash-handle or leak. The ffmpeg complexity lives in
    a script rather than inline YAML because the command needs nested shell + filter quoting, and a script
    can be run by hand to debug a stream that won't clamp.
    """
    p = _clamp_params(cfg)
    encoder = _clamp_encoder(cfg)
    rtsp = _rtsp_port(cfg)
    secret = clamp_secret(cfg)
    try:
        from app.services.media_service import resolve_ffmpeg, _render_node
        ffmpeg = resolve_ffmpeg()
        vaapi_dev = _render_node() if encoder == "h264_vaapi" else ""
    except Exception:
        ffmpeg, vaapi_dev = "ffmpeg", ""
    # ffprobe lives beside ffmpeg in every build we resolve (system package or a bundled tree), so derive
    # it from the resolved path rather than trusting PATH — the service PATH is polluted enough that
    # resolve_ffmpeg exists precisely because of it.
    import os.path as _osp
    ffprobe = _osp.join(_osp.dirname(ffmpeg), "ffprobe") if _osp.dirname(ffmpeg) else "ffprobe"
    ceiling_kbps = _rate_kbps(p["vbitrate"])
    # Resolved to a PATH here: `{quality_dir}` in the script f-string below would interpolate the
    # FUNCTION object (it did — the generated script had a literal `<function quality_dir at 0x…>` as
    # its path, which sh -n happily accepts because it is syntactically a fine string).
    qdir = quality_dir()
    audio_kbps = _rate_kbps(p["abitrate"], default=128)
    hw_pre, hw_post = _clamp_video_args(encoder, p)
    sw_pre, sw_post = _clamp_video_args("libx264", p)
    # Per-tier run functions, args written LITERALLY for exactly the reason the comment above run_hw gives:
    # these filter strings carry nested single quotes, so a fragment expanded from a shell variable would
    # reach ffmpeg with its quote characters as data. One function per (tier x encoder) keeps the shell
    # parsing them, and the dispatch below picks one. Each tier is capped to the admin params — min() on
    # every axis — so a tier can only lower the ceiling, never raise it.
    tier_fns, tier_cases, tier_ceils = [], [], []
    for _name, (_h, _f, _vb) in QUALITY_TIERS.items():
        _tp = dict(p)
        _tp["height"] = min(int(p["height"]), _h)
        _tp["fps"] = min(int(p["fps"]), _f)
        _tp["vbitrate"] = _vb if _rate_kbps(_vb) < ceiling_kbps else p["vbitrate"]
        _hp, _hq = _clamp_video_args(encoder, _tp)
        _sp, _sq = _clamp_video_args("libx264", _tp)
        tier_fns.append(
            f"run_hw_{_name}() {{\n"
            f"  # shellcheck disable=SC2086\n"
            f"  exec \"$FFMPEG\" {_hp} $COMMON -i \"$IN\" {_hq} $OUTOPTS \"$OUT\"\n"
            f"}}\n"
            f"run_sw_{_name}() {{\n"
            f"  # shellcheck disable=SC2086\n"
            f"  exec \"$FFMPEG\" {_sp} $COMMON -i \"$IN\" {_sq} $OUTOPTS \"$OUT\"\n"
            f"}}")
        tier_cases.append(f"  {_name}) run_hw_{_name} ;;")
        tier_ceils.append(f"  {_name}) VMAX_CFG={min(_rate_kbps(_vb), ceiling_kbps)} ;;")
    tier_funcs = "\n".join(tier_fns)
    tier_hw_case = "\n".join(tier_cases)
    tier_sw_case = "\n".join(c.replace("run_hw_", "run_sw_") for c in tier_cases)
    tier_ceil_case = "\n".join(tier_ceils)
    # `-map 0:a:0?` — the trailing ? makes audio OPTIONAL. A screen share published with no microphone has
    # no audio track at all, and a non-optional map aborts ffmpeg outright ("Stream map ... does not exist"),
    # which would leave that stream permanently unwatchable instead of merely silent.
    maps = "-map 0:v:0 -map 0:a:0?"
    script = f"""#!/bin/sh
# GENERATED by app/services/stream_service.py — edits are overwritten on every MediaMTX (re)start.
# Live bitrate clamp: read the source stream, re-encode to a fixed ceiling, publish it back as
# <path>{CLAMP_SUFFIX}. Viewers are served the clamped path (see app/routers/streams.py), so what a
# streamer's encoder sends can never dictate what every viewer downloads.
# Run by hand to debug:  streamserver/clamp.sh <stream-token>
#
# -f (noglob) matters: the unquoted $OUTOPTS below contains `-map 0:a:0?` and the shell would otherwise try
# to pathname-expand that `?` against the working directory.
set -uf
SRC="${{1:-}}"
[ -n "$SRC" ] || {{ echo "clamp: no path given" >&2; exit 1; }}
case "$SRC" in
  # Never clamp a clamped path: MediaMTX fires runOnReady for our own output too, and without this we would
  # transcode in an endless chain. The generated config also routes clamped paths to a separate entry with
  # no runOnReady, so this is belt-and-braces (and protects anyone running the script by hand).
  *{CLAMP_SUFFIX}) echo "clamp: refusing to clamp an already-clamped path" >&2; exit 0 ;;
esac

FFMPEG="{ffmpeg}"
FFPROBE="{ffprobe}"
VAAPI_DEVICE="{vaapi_dev}"
IN="rtsp://127.0.0.1:{rtsp}/$SRC"
# ?clamp=… is how /api/streams/auth recognises this publish as ours; see stream_service.clamp_secret.
OUT="rtsp://127.0.0.1:{rtsp}/${{SRC}}{CLAMP_SUFFIX}?clamp={secret}"
COMMON="-nostdin -hide_banner -loglevel warning -fflags nobuffer -rtsp_transport tcp"

# ---- effective bitrate ceiling -------------------------------------------------------------------
# NEVER spend more than the source does. Re-encoding is not free: a 304 kbit/s phone stream measured
# 1447 kbit/s out at a 1500k ceiling — a 4.8x INFLATION — and every rate-control mode does this, because
# the artefacts in a low-bitrate picture are detail the encoder has to spend bits reproducing. So the
# ceiling is min(measured source, configured), which leaves a fat OBS stream fully clamped while making a
# weak phone stream roughly bandwidth-neutral instead of far worse.
VMAX_CFG={ceiling_kbps}      # configured ceiling, kbit/s (lowered by the streamer's tier below)
# The streamer's own quality choice, read at stream START from a file keyed by token (see
# stream_service.set_quality). Anything not a known tier — including no file at all — means AUTO, i.e. the
# node's configured ceiling. Read defensively: this file is written by the app and only ever contains a
# short tier name, but it is shell input, so strip it to the characters a tier can contain.
Q=auto
QFILE="{qdir}/$SRC"
if [ -f "$QFILE" ]; then
  Q=$(tr -cd 'a-z0-9' < "$QFILE" 2>/dev/null || echo auto)
  [ -n "$Q" ] || Q=auto
fi
case "$Q" in
{tier_ceil_case}
  *) Q=auto ;;
esac
[ "$Q" = auto ] || echo "clamp: $SRC using the streamer's ${{Q}}p tier (ceiling $VMAX_CFG kbit/s)" >&2
AUD_CFG={audio_kbps}         # configured AAC bitrate; scaled DOWN on weak sources (see below)
VMIN=150                     # never target something absurd if the probe reads very low
AUD_MIN=48                   # below this speech stops being intelligible
SETTLE=15                    # seconds to let the publisher's bitrate settle before measuring

# WebRTC bandwidth estimation ramps UP over many seconds, so the opening of a WHIP publish is NOT
# representative. Measured against a real phone: steady state 1489 kbit/s, but sampling at t=3-7s read
# 807 — a 1.8x underestimate that pinned the ceiling BELOW what the phone was offering and softened the
# picture for the whole session (a stable stream never restarts, so it never re-measures).
#
# Hence a long settle rather than a big headroom. Waiting is close to free: a stream has almost no
# viewers in its first seconds, and those it does have are served the source meanwhile (see
# _upstream_path). It also costs nothing where it would matter most — OBS publishes at full rate from
# the first frame, so the wait only ever delays tightening a stream that was never going to be tightened.
# Headroom can then stay small, which is what keeps a genuinely weak source from being inflated.

measure_src_kbps() {{
  # Copy a few seconds off the source (no decode) and divide bytes by duration. ffprobe's own bit_rate is
  # unreliable on a live RTSP feed, so measure the bytes that actually arrive.
  #
  # matroska, NOT mp4: a WHIP publisher can negotiate VP8, which mp4 cannot carry at all ("Could not find
  # tag for codec vp8") — the remux writes 0 bytes, the measurement reads 0, and we'd silently fall back to
  # the configured ceiling on exactly the low-bitrate phone streams this measurement exists to protect.
  # Matroska carries anything the ingest can hand us (H264/VP8/AV1 + Opus/AAC).
  _t=$(mktemp "${{TMPDIR:-/tmp}}/pcai-clamp-XXXXXX") || return 1
  trap 'rm -f "$_t"' EXIT INT TERM      # MediaMTX kills this process when the source stops; don't leak
  "$FFMPEG" -nostdin -hide_banner -loglevel error -rtsp_transport tcp -i "$IN" \\
      -t 4 -c copy -f matroska -y "$_t" >/dev/null 2>&1
  _b=$(wc -c < "$_t" 2>/dev/null || echo 0)
  _d=$("$FFPROBE" -v error -show_entries format=duration -of csv=p=0 "$_t" 2>/dev/null)
  rm -f "$_t"
  trap - EXIT INT TERM
  awk -v b="$_b" -v d="$_d" 'BEGIN{{ if (d+0 > 0.5) printf "%d", b*8/d/1000; else print 0 }}'
}}

sleep "$SETTLE"
SRC_KBPS=$(measure_src_kbps)
if [ "${{SRC_KBPS:-0}}" -gt 0 ]; then
  # Audio scales down with the source. At the configured 128k a 200 kbit/s stream would spend nearly
  # two-thirds of its budget on audio and leave the picture unwatchable; cap it at a quarter of the
  # budget instead. Never below AUD_MIN, never above what the admin configured.
  AUD=$AUD_CFG
  if [ "$SRC_KBPS" -lt $((AUD_CFG * 4)) ]; then
    AUD=$((SRC_KBPS / 4))
    [ "$AUD" -lt "$AUD_MIN" ] && AUD=$AUD_MIN
    [ "$AUD" -gt "$AUD_CFG" ] && AUD=$AUD_CFG
  fi
  # 1.25x headroom, minus the audio we're about to spend. Small on purpose: the settle above is what
  # handles ramp-up, so this only has to absorb ordinary variation. A large headroom would instead
  # re-inflate weak sources, which is the thing this whole measurement exists to prevent.
  VMAX=$(( SRC_KBPS * 5 / 4 - AUD ))
  [ "$VMAX" -lt "$VMIN" ] && VMAX=$VMIN
  [ "$VMAX" -gt "$VMAX_CFG" ] && VMAX=$VMAX_CFG
  echo "clamp: $SRC_KBPS kbit/s in -> video $VMAX + audio $AUD kbit/s (configured $VMAX_CFG/$AUD_CFG)" >&2
else
  # Could not measure (source still warming up, ffprobe missing) — fall back to the configured values
  # rather than guessing low, which would visibly wreck a stream that is actually fine.
  VMAX=$VMAX_CFG
  AUD=$AUD_CFG
  echo "clamp: could not measure $SRC — using the configured ceiling $VMAX kbit/s" >&2
fi
VBUF=$((VMAX * 2))           # 2x: a 1x buffer is effectively CBR and pads the bitrate back up
OUTOPTS="{maps} -c:a aac -b:a ${{AUD}}k -ar 48000 -f rtsp -rtsp_transport tcp"

# The encoder args are written out LITERALLY in each function (not passed as "$1") so that the shell parses
# their quoting. A fragment expanded from a variable keeps its quote characters as data, and ffmpeg would
# then be handed a filter string with literal `"` in it.
run_hw_auto() {{
  # shellcheck disable=SC2086
  exec "$FFMPEG" {hw_pre} $COMMON -i "$IN" {hw_post} $OUTOPTS "$OUT"
}}
run_sw_auto() {{
  # shellcheck disable=SC2086
  exec "$FFMPEG" {sw_pre} $COMMON -i "$IN" {sw_post} $OUTOPTS "$OUT"
}}
{tier_funcs}
# Dispatch on the streamer's tier. `auto` (and anything unrecognised) is the node's configured clamp, so a
# stream that never asked for a tier behaves exactly as it did before this existed.
run_hw() {{
  case "$Q" in
{tier_hw_case}
  *) run_hw_auto ;;
  esac
}}
run_sw() {{
  case "$Q" in
{tier_sw_case}
  *) run_sw_auto ;;
  esac
}}

# Ask the hardware encoder DIRECTLY whether it works, with a throwaway encode of a few synthetic frames.
#
# The obvious alternative — "if the real transcode died within N seconds, assume the encoder is broken and
# drop to CPU" — is wrong, and was measured to be wrong in production: a WHIP/phone publisher renegotiates
# a second or two after going live, which kills the SOURCE and takes our ffmpeg down with it. That looks
# exactly like a failing encoder, so a perfectly good GPU stream got demoted to libx264 (46% of a core) for
# its whole duration. Runtime cannot distinguish "encoder unusable" from "publisher blipped"; this probe
# tests only the thing we actually want to know, and costs ~100ms once per stream.
# Probes with the REAL encoder arguments, not just `-c:v <encoder>`: rate-control flags are spelled
# differently per encoder and a wrong one is rejected at open time. Probing the full set means a bad
# combination falls back to the CPU (with a log line) instead of failing forever on every restart.
hw_ok() {{
  "$FFMPEG" -hide_banner -loglevel error {hw_pre} \\
    -f lavfi -i testsrc=size=256x144:rate=5:duration=0.2 \\
    {hw_post} -f null - >/dev/null 2>&1
}}

START=$(date +%s)
if [ "{encoder}" = "libx264" ] || hw_ok; then
  # Any later failure is a source-side problem, NOT the encoder — exit and let MediaMTX's
  # runOnReadyRestart bring us back, which re-probes and stays on hardware.
  ( run_hw ) && exit 0
else
  echo "clamp: {encoder} is not usable on this node — encoding $SRC on the CPU instead" >&2
  ( run_sw ) && exit 0
fi

# Reaching here means the transcode failed outright (no ffmpeg on this node, the encoder died, …).
# MediaMTX restarts a runOnReady command as soon as it exits and applies no backoff of its own, so an
# instant failure would respawn us in a tight loop for the entire length of the stream. Hold the exit down
# to one attempt every few seconds. Viewers are unaffected either way: with no clamped path published, the
# HLS proxy falls back to serving the source (see _upstream_path).
END=$(date +%s)
[ $((END - START)) -lt 5 ] && sleep 5
echo "clamp: transcode attempt for $SRC ended — viewers get the unclamped source until it recovers" >&2
exit 1
"""
    _CLAMP_SCRIPT.write_text(script)
    try:
        _CLAMP_SCRIPT.chmod(0o755)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[stream] could not chmod the clamp script: %s", e)
    logger.info("[stream] clamp: %sp%s @ %s via %s", p["height"], p["fps"], p["vbitrate"], encoder)


def _cfg_sig(cfg: dict) -> str:
    return "|".join((cfg.get(k, "") or "") for k in _SIG_KEYS) + "|" + _app_port()


def _bin_ok() -> bool:
    """True only for a real (non-empty) binary — the Docker build may leave an empty placeholder when the
    MediaMTX download fails, and we must treat that as 'not installed' rather than exec a 0-byte file."""
    try:
        return _STREAM_BIN.exists() and _STREAM_BIN.stat().st_size > 0
    except Exception:
        return False


def _wanted(cfg: dict) -> bool:
    return _enabled(cfg) and _bin_ok()


def _running() -> bool:
    return _proc is not None and _proc.poll() is None


def _write_config(cfg: dict) -> None:
    """Render a minimal mediamtx.yml: RTMP ingest + HLS output, external HTTP auth against the app.

    Playback (HLS reads) is public; publishing is gated by the /api/streams/auth hook (API-key check).
    Kept intentionally small — only the transports we use are enabled.
    """
    rtmp_port = (cfg.get("stream_rtmp_port", "") or "1935").strip()
    hls_port = (cfg.get("stream_hls_port", "") or "8888").strip()
    srt_port = (cfg.get("stream_srt_port", "") or "").strip()
    webrtc_port = (cfg.get("stream_webrtc_port", "") or "8889").strip()
    webrtc_udp = (cfg.get("stream_webrtc_udp_port", "") or "8189").strip()
    # Public host advertised in WebRTC ICE candidates so a phone can reach the server's media port.
    # Sanitize to hostname/IP characters only — it's interpolated into the YAML, so a stray quote/bracket
    # (accidental or malicious) must not be able to inject config keys or produce an unparseable file.
    import re
    pub_host = (cfg.get("stream_domain", "") or "").strip() or (cfg.get("turn_public_ip", "") or "").strip()
    pub_host = re.sub(r"[^A-Za-z0-9.:\-]", "", pub_host)
    secret = (cfg.get("stream_auth_secret", "") or "").strip()
    auth_url = f"http://127.0.0.1:{_app_port()}/api/streams/auth?hook={secret}"
    # Config keys target the pinned MediaMTX v1.19.2 (see install.sh / Dockerfile MEDIAMTX_VERSION).
    api_port = (cfg.get("stream_api_port", "") or "9997").strip()
    clamp = clamp_enabled(cfg)
    if clamp:
        _write_clamp_script(cfg)
    lines = [
        "logLevel: info",
        "logDestinations: [stdout]",
        # Control API, bound to LOOPBACK only: stream_end_service asks it whether a path is still publishing.
        # It's the only liveness signal that's correct for BOTH ingests — the HLS playlist 404s for a
        # WebRTC/WHIP (phone) stream that hasn't warmed up, and a dead MediaMTX 404s for everything.
        "api: yes",
        f"apiAddress: 127.0.0.1:{api_port}",
        "metrics: no",
        "pprof: no",
        "playback: no",
        # RTSP, bound to LOOPBACK only — it is not a public ingest/playback transport here, it exists so the
        # bitrate clamp can read a source stream and publish the transcoded copy back (see clamp.sh). Left
        # off entirely when the clamp is disabled so we open no port we don't use.
        # rtspTransports MUST be pinned to tcp: RTSP otherwise also opens UDP RTP/RTCP listeners on
        # :8000/:8001 across ALL interfaces (verified with v1.19.2) — two publicly-bound ports we never use,
        # since clamp.sh reads and writes over TCP. Pinning the transport removes them entirely.
        *(["rtsp: yes", f"rtspAddress: 127.0.0.1:{_rtsp_port(cfg)}", "rtspEncryption: \"no\"",
           "rtspTransports: [tcp]"] if clamp else ["rtsp: no"]),
        "rtmp: yes",
        f"rtmpAddress: :{rtmp_port}",
        "rtmpEncryption: \"no\"",
        "hls: yes",
        f"hlsAddress: :{hls_port}",
        "hlsAllowOrigins: [\"*\"]",
        # fmp4, NOT mpegts. A phone goes live over WebRTC/WHIP, which produces Opus audio (and, unless we
        # force H264, VP8 video) — the mpegts HLS variant supports ONLY H264 + AAC, so its muxer was
        # destroyed on creation ("supports MPEG-4 Audio only") and viewers got 500/404 with no playlist.
        # fmp4 carries Opus + H264, so both the phone (H264+Opus) and OBS (H264+AAC) paths remux to a
        # playlist hls.js can play. (The client always plays via hls.js, which supports fmp4+Opus.)
        "hlsVariant: fmp4",
        "hlsSegmentCount: 7",
        "hlsSegmentDuration: 2s",
        # WebRTC/WHIP ingest — lets a phone go live straight from the browser (PWA/app) via getUserMedia.
        "webrtc: yes",
        f"webrtcAddress: :{webrtc_port}",
        f"webrtcLocalUDPAddress: :{webrtc_udp}",
        "webrtcAllowOrigins: [\"*\"]",
        (f"webrtcAdditionalHosts: [\"{pub_host}\"]" if pub_host else "webrtcAdditionalHosts: []"),
        # MoQ (new in v1.19) auto-binds :8892 and we don't use it — turn it off so it needs no port.
        "moq: no",
    ]
    if srt_port:
        lines += ["srt: yes", f"srtAddress: :{srt_port}"]
    else:
        lines += ["srt: no"]
    # The instant the publisher (OBS / the phone) drops, tell the app so it can publish the streamer's parked
    # "ended" event — their kind-30311 is signed in the browser, so a closed tab would otherwise leave the
    # stream announced as ● LIVE forever (see app/services/stream_end_service.py). MediaMTX runs this through
    # `sh -c`, so $MTX_PATH (the stream token) expands. curl ships with every supported install (it's what
    # fetches MediaMTX itself); if it's ever missing, the reaper sweep still ends the stream.
    # NOTE the key: MediaMTX v1 renamed runOnPublish/runOnUnpublish → runOnReady/runOnNotReady, and it
    # REJECTS an unknown field outright ("json: unknown field") — a wrong name here is a crash-loop, not a
    # silently-ignored setting.
    end_url = f"http://127.0.0.1:{_app_port()}/api/streams/unpublish?hook={secret}&path=$MTX_PATH"
    # External HTTP auth: MediaMTX POSTs {action, path, query, ...}; the app allows reads, gates publishes.
    lines += [
        "authMethod: http",
        f"authHTTPAddress: {auth_url}",
        "authHTTPExclude:",
        # The control API is bound to 127.0.0.1 only and is what stream_end_service probes for liveness.
        # Without this it inherits authMethod:http, our hook denies the unknown "api" action, and every probe
        # comes back 401 — which the probe (correctly) reports as "can't tell", so no stream would ever end.
        "  - action: api",
        "  - action: metrics",
        "  - action: pprof",
        "",
        "paths:",
    ]
    if clamp:
        # The clamp's OWN output path, matched by regex BEFORE all_others (MediaMTX tries exact paths, then
        # regex paths in config order, then all_others). It deliberately inherits none of all_others':
        #   - no runOnReady    — otherwise MediaMTX would clamp our clamp, forever.
        #   - no runOnNotReady — the source path already reports the end. Firing it here would ask the app to
        #                        end a stream keyed by "<token>_clamped", and when the SOURCE later resumes
        #                        (an OBS reconnect blip) the end is already scheduled against a name nothing
        #                        can re-confirm as live.
        #   - no record        — recording stays on the SOURCE, so "Past streams" VODs keep the full-quality
        #                        original and stream_vod_service's <rec_dir>/<token>/ layout is unchanged.
        lines += [
            f"  \"~^.*{CLAMP_SUFFIX}$\":",
            "    record: no",
        ]
    lines += [
        "  all_others:",
        f"    runOnNotReady: 'curl -sS -m 5 -o /dev/null -X POST \"{end_url}\"'",
    ]
    if clamp:
        # Live bitrate clamp: MediaMTX starts this when a source goes live and kills it when the source
        # stops, so the transcode needs no supervisor of its own. runOnReadyRestart respawns ffmpeg if it
        # dies while the source is still up (clamp.sh handles the "hardware encoder is unusable" case
        # itself, so a restart loop can't be caused by a bad encoder choice).
        lines += [
            f"    runOnReady: '{_CLAMP_SCRIPT} \"$MTX_PATH\"'",
            "    runOnReadyRestart: yes",
        ]
    # Save-to-Blossom recording: MediaMTX records each publish as fmp4 into a temp dir; stream_vod_service
    # uploads it to the streamer's Blossom drive when the stream ends and deletes it. Files land at
    # <rec_dir>/<token>/<timestamp>.mp4. (Mount rec_dir as tmpfs for RAM-backed / no-SSD recording.)
    if (cfg.get("stream_record_enabled", "") or "").strip().lower() == "true":
        rec_dir = recording_dir()
        try:
            os.makedirs(rec_dir, exist_ok=True)
        except Exception as e:
            logger.warning("[stream] could not create recording dir %s: %s", rec_dir, e)
        lines += [
            "    record: yes",
            f"    recordPath: '{rec_dir}/%path/%Y-%m-%d_%H-%M-%S-%f'",
            "    recordFormat: fmp4",
            "    recordSegmentDuration: '24h'",   # one file per session unless it runs a full day
            "    recordDeleteAfter: '0s'",         # 0 duration = never auto-delete; stream_vod_service cleans
        ]                                          # up after uploading. (MediaMTX wants a DURATION STRING here,
        #                                            not a bare number — `0` → "cannot unmarshal number".)
    lines.append("")
    _STREAM_CFG.write_text("\n".join(lines) + "\n")


"""A MediaMTX that outlives the app that started it is the worst kind of running.

`_terminate` acts on `_proc`, an in-process handle, so it only ever fires on a CLEAN shutdown. A
SIGKILL, a crash, an OOM — anything that stops the app without running its shutdown hook — leaves
MediaMTX alive, and the next app process starts with `_proc = None` and no idea it is there.

That is not a leak, it is a silent configuration freeze. The survivor keeps serving on the config it
loaded at ITS start, so every later change to `mediamtx.gen.yml` deploys, verifies, and does
nothing. Found in production: an instance from 13:28 survived several restarts, which is why
`runOnReady` — the bitrate clamp — never applied, and viewers pulled the streamer's full source
bitrate off a residential uplink and buffered. It was invisible because `logDestinations: [stdout]`
was writing into the dead parent's pipe: MediaMTX's log existed and reached nobody.

Two defences, because they cover different failures:
  * PDEATHSIG — the kernel sends the child SIGTERM when THIS process dies, however it dies. This is
    the one that actually prevents an orphan.
  * a pidfile checked at spawn — for an orphan that already exists (one started before this fix, or
    one whose PDEATHSIG did not apply). The cmdline is verified before signalling, because a pid is
    reused and killing an unrelated process is worse than the bug.
"""
_PIDFILE = _STREAM_DIR / "mediamtx.pid"


def _pdeathsig() -> None:      # pragma: no cover - runs between fork and exec
    try:
        import ctypes
        import signal as _sig
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, _sig.SIGTERM)   # PR_SET_PDEATHSIG
    except Exception:
        pass


def _kill_stale() -> None:
    """Kill a MediaMTX left behind by a previous app process, before spawning ours."""
    try:
        pid = int(_PIDFILE.read_text().strip())
    except Exception:
        return
    if pid <= 0:
        return
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "ignore")
    except Exception:
        _clear_pidfile()
        return
    # The pid must still BE mediamtx. Pids are reused, and killing whatever inherited this one is a
    # far worse bug than the one being fixed.
    if str(_STREAM_BIN) not in cmdline:
        _clear_pidfile()
        return
    logger.warning("[stream] a mediamtx from a previous app process is still running (pid %s); "
                   "stopping it — it would keep serving the config IT started with", pid)
    import signal as _sig
    try:
        os.kill(pid, _sig.SIGTERM)
        for _ in range(30):                     # up to 3s for a clean exit
            time.sleep(0.1)
            os.kill(pid, 0)                     # raises once it is gone
        os.kill(pid, _sig.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    except Exception as e:
        logger.warning("[stream] could not stop the stale mediamtx: %s", e)
    _clear_pidfile()


def _clear_pidfile() -> None:
    try:
        _PIDFILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def _spawn(cfg: dict) -> None:
    global _proc
    try:
        _write_config(cfg)
        _kill_stale()          # before binding: a survivor already holds :1935/:8888
        _proc = subprocess.Popen([str(_STREAM_BIN), str(_STREAM_CFG)], cwd=str(_STREAM_DIR),
                                 preexec_fn=_pdeathsig)
        try:
            _PIDFILE.write_text(str(_proc.pid))
        except Exception as e:
            logger.warning("[stream] could not write the mediamtx pidfile: %s", e)
        logger.info("[stream] started mediamtx (pid %s): rtmp :%s, hls :%s", _proc.pid,
                    cfg.get("stream_rtmp_port", "1935"), cfg.get("stream_hls_port", "8888"))
    except Exception as e:
        logger.error("[stream] failed to spawn mediamtx: %s", e)
        _proc = None


def _terminate() -> None:
    global _proc
    if _proc is None:
        return
    try:
        if _proc.poll() is None:
            _proc.terminate()
            try:
                _proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _proc.kill()
    except Exception:
        pass
    _proc = None
    _clear_pidfile()


def _reconcile() -> None:
    global _restart_count, _restart_window_start, _gaveup_logged, _spawn_sig
    with _lock:
        if _stop_event.is_set():
            return
        cfg = _cfg()
        if not _wanted(cfg):
            if _running():
                logger.info("[stream] streaming disabled — stopping mediamtx")
                _terminate()
            _restart_count = 0
            _gaveup_logged = False
            _spawn_sig = None
            return
        # Resolve (+ persist once) the auth-hook secret so the config + the signature + the endpoint agree.
        cfg["stream_auth_secret"] = _ensure_hook_secret(cfg)
        sig = _cfg_sig(cfg)
        if _running():
            if sig == _spawn_sig:
                return
            logger.info("[stream] stream settings changed — restarting mediamtx to apply")
            _terminate()
        now = time.time()
        if now - _restart_window_start > _RESTART_WINDOW:
            _restart_window_start = now
            _restart_count = 0
            _gaveup_logged = False
        if _restart_count >= _MAX_RESTARTS_PER_HOUR:
            if not _gaveup_logged:
                logger.error("[stream] mediamtx restart cap hit (%d/hr) — parking until next window",
                             _MAX_RESTARTS_PER_HOUR)
                _gaveup_logged = True
            return
        _restart_count += 1
        _spawn(cfg)
        _spawn_sig = sig


def _monitor_loop() -> None:
    while not _stop_event.is_set():
        try:
            _reconcile()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[stream] reconcile error: %s", e)
        _stop_event.wait(_RECONCILE_INTERVAL)


def start_stream_server() -> None:
    """Idempotently start the supervisor thread (which starts/stops mediamtx per settings)."""
    global _monitor_thread
    with _lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        if not _bin_ok():
            logger.info("[stream] mediamtx binary not present (%s) — OBS streaming disabled "
                        "(install it with install.sh --stream to enable)", _STREAM_BIN)
            return
        _stop_event.clear()
        _monitor_thread = threading.Thread(target=_monitor_loop, name="stream-monitor", daemon=True)
        _monitor_thread.start()
        logger.info("[stream] supervisor started")


def stop_stream_server() -> None:
    """Stop the supervisor + terminate mediamtx (kept under the ~3s service-stop deadline)."""
    global _monitor_thread
    _stop_event.set()
    with _lock:
        _terminate()
    t = _monitor_thread
    if t is not None:
        t.join(timeout=3)
    _monitor_thread = None
    logger.info("[stream] supervisor stopped")
