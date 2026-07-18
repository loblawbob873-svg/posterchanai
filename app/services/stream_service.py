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
             "stream_record_enabled", "stream_record_dir")


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
        "rtsp: no",
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
        "  all_others:",
        f"    runOnNotReady: 'curl -sS -m 5 -o /dev/null -X POST \"{end_url}\"'",
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


def _spawn(cfg: dict) -> None:
    global _proc
    try:
        _write_config(cfg)
        _proc = subprocess.Popen([str(_STREAM_BIN), str(_STREAM_CFG)], cwd=str(_STREAM_DIR))
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
