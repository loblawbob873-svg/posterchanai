"""Supervise the built-in LiveKit SFU (livekit/livekit-server) for Concord calls (CORD-07).

Concord voice/video is a LiveKit room reached through a "blind broker" (app/routers/concord_av.py): the broker
proves a caller holds the channel's room key and hands back a short LiveKit token; it never learns who anyone is.
Without our own, every call from this instance went through armada.buzz's. This is the same supervisor shape as
turn_service: a monitor thread spawns the binary from the `livekit_*` settings, restarts it on crash under an
hourly cap, and applies a settings change by respawning -- no service restart.

Why it is cheap to run: media is end-to-end encrypted per sender (CORD-07), so the SFU only FORWARDS frames --
never decodes or transcodes. One UDP port carries every call (rtc.udp_port, LiveKit's UDP mux), TCP is the
fallback for networks that drop UDP, no Redis (one node), logging at warn, no Prometheus. Idle it is a sleeping
Go process. Clients behind a strict NAT get our pion TURN (turn_service), sharing its REST secret.

Turnkey notes:
- The binary is fetched by `install.sh --livekit` (pinned version + sha256); absent, this is a silent no-op and the
  broker answers "this server does not provide Concord calls", so clients keep using another broker.
- `livekit_api_key` / `livekit_api_secret` are generated once and persisted; the broker signs tokens with them.
- Signalling listens on 127.0.0.1 only; nginx proxies wss://<host>/livekit/ to it.
- Runs only on the port-3051 instance (app/main.py, the 'media' role), like TURN and MediaMTX.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from app.services import settings_store

logger = logging.getLogger(__name__)

_DIR = Path(__file__).resolve().parents[2] / "livekit"
_BIN = _DIR / "livekit-server"
_CONF = _DIR / "livekit.gen.yaml"

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

DEFAULTS = {"livekit_http_port": "7880", "livekit_tcp_port": "7881", "livekit_udp_port": "7882"}


def _cfg() -> dict:
    return settings_store.all_settings()


def enabled(cfg: dict) -> bool:
    return (cfg.get("livekit_enabled", "false") or "").strip().lower() == "true"


def _port(cfg: dict, key: str) -> int:
    raw = str(cfg.get(key, "") or "").strip() or DEFAULTS[key]
    try:
        v = int(raw)
    except ValueError:
        v = int(DEFAULTS[key])
    return v if 1 <= v <= 65535 else int(DEFAULTS[key])


def public_ip(cfg: dict) -> str:
    """The address advertised in ICE candidates: livekit_public_ip, else the TURN one (the same router)."""
    ip = (cfg.get("livekit_public_ip", "") or "").strip() or (cfg.get("turn_public_ip", "") or "").strip()
    return ip if re.fullmatch(r"[0-9A-Fa-f.:]{2,45}", ip) else ""


def _ensure(cfg: dict, key: str, make) -> str:
    v = (cfg.get(key, "") or "").strip()
    if v:
        return v
    v = make()
    try:
        settings_store.put(key, v)
        logger.info("[livekit] generated %s on first start", key)
    except Exception as e:  # pragma: no cover - persistence best-effort
        logger.warning("[livekit] could not persist %s: %s", key, e)
    return v


def credentials(cfg: Optional[dict] = None) -> tuple[str, str]:
    """(api_key, api_secret), generated and persisted the first time anything asks."""
    cfg = cfg if cfg is not None else _cfg()
    key = _ensure(cfg, "livekit_api_key", lambda: "PC" + secrets.token_hex(8))
    secret = _ensure(cfg, "livekit_api_secret", lambda: secrets.token_urlsafe(48))
    return key, secret


def render_config(cfg: dict, api_key: str, api_secret: str) -> str:
    """The YAML LiveKit runs with. Pure, so tests can read exactly what ships."""
    lines = [
        f"port: {_port(cfg, 'livekit_http_port')}",
        "bind_addresses: [\"127.0.0.1\"]",
        "rtc:",
        f"  tcp_port: {_port(cfg, 'livekit_tcp_port')}",
        f"  udp_port: {_port(cfg, 'livekit_udp_port')}",
        "  use_external_ip: false",
    ]
    ip = public_ip(cfg)
    if ip:
        lines.append(f"  node_ip: {ip}")
    turn_on = (cfg.get("turn_enabled", "") or "").strip().lower() == "true"
    turn_secret = (cfg.get("turn_shared_secret", "") or "").strip()
    turn_host = (cfg.get("turn_domain", "") or "").strip() or (cfg.get("turn_public_ip", "") or "").strip()
    if turn_on and turn_secret and re.fullmatch(r"[A-Za-z0-9.:-]+", turn_host or ""):
        turn_port = str(cfg.get("turn_port", "") or "3478").strip()
        lines += ["  turn_servers:", f"    - host: {turn_host}", f"      port: {int(turn_port) if turn_port.isdigit() else 3478}",
                  "      protocol: udp", f"      secret: \"{turn_secret}\"", "      ttl: 14400"]
    lines += [
        "keys:",
        f"  {api_key}: \"{api_secret}\"",
        "logging:",
        "  level: warn",
        "room:",
        "  empty_timeout: 60",
        "  departure_timeout: 20",
    ]
    return "\n".join(lines) + "\n"


def installed() -> bool:
    """A real binary: the Docker image leaves a 0-byte placeholder when its download failed."""
    try:
        return _BIN.is_file() and _BIN.stat().st_size > 0
    except OSError:
        return False


def wanted(cfg: dict) -> bool:
    return enabled(cfg) and installed() and bool(public_ip(cfg))


def available() -> bool:
    """Is a call SFU running here right now (what the broker's discovery probe answers)."""
    return _proc is not None and _proc.poll() is None


def _write_config(text: str) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CONF.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, _CONF)


def _spawn(cfg: dict) -> None:
    global _proc
    try:
        _proc = subprocess.Popen([str(_BIN), "--config", str(_CONF)], cwd=str(_DIR),
                                 stdin=subprocess.DEVNULL, close_fds=True)
        logger.info("[livekit] started livekit-server (pid %s): udp %s, tcp %s", _proc.pid,
                    _port(cfg, "livekit_udp_port"), _port(cfg, "livekit_tcp_port"))
    except Exception as e:
        logger.error("[livekit] failed to spawn livekit-server: %s", e)
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
        if not wanted(cfg):
            if available():
                logger.info("[livekit] calls disabled/misconfigured -- stopping livekit-server")
                _terminate()
            _restart_count = 0
            _gaveup_logged = False
            _spawn_sig = None
            return
        key, secret = credentials(cfg)
        text = render_config(cfg, key, secret)
        if available():
            if text == _spawn_sig:
                return
            logger.info("[livekit] settings changed -- restarting livekit-server to apply")
            _terminate()
        now = time.time()
        if now - _restart_window_start > _RESTART_WINDOW:
            _restart_window_start = now
            _restart_count = 0
            _gaveup_logged = False
        if _restart_count >= _MAX_RESTARTS_PER_HOUR:
            if not _gaveup_logged:
                logger.error("[livekit] restart cap hit (%d/hr) -- parking until next window", _MAX_RESTARTS_PER_HOUR)
                _gaveup_logged = True
            return
        _restart_count += 1
        _write_config(text)
        _spawn(cfg)
        _spawn_sig = text


def _monitor_loop() -> None:
    while not _stop_event.is_set():
        try:
            _reconcile()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[livekit] reconcile error: %s", e)
        _stop_event.wait(_RECONCILE_INTERVAL)


def start_livekit_server() -> None:
    global _monitor_thread
    with _lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        if not installed():
            logger.info("[livekit] livekit-server not installed (%s) -- Concord calls use other brokers "
                        "(install with install.sh --livekit)", _BIN)
            return
        _stop_event.clear()
        _monitor_thread = threading.Thread(target=_monitor_loop, name="livekit-monitor", daemon=True)
        _monitor_thread.start()
        logger.info("[livekit] supervisor started")


def stop_livekit_server() -> None:
    global _monitor_thread
    _stop_event.set()
    with _lock:
        _terminate()
    t = _monitor_thread
    if t is not None:
        t.join(timeout=3)
    _monitor_thread = None
    logger.info("[livekit] supervisor stopped")
