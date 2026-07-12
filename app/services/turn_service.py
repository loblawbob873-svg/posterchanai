"""Supervise the built-in Pion TURN/STUN relay (turnserver/pion-turn) as a subprocess.

This is the in-app supervisor for the voice/video call feature's NAT relay — the modern, config-free
replacement for coturn. It mirrors the botframework supervisor (bot_manager_service): a monitor thread
reconciles the desired state every few seconds, (re)spawning the Go binary with env from the `turn_*`
settings and restarting it on crash with an hourly cap. Toggling `turn_enabled` in Admin takes effect on
the next reconcile — no restart needed.

Turnkey notes:
- The binary is built by `install.sh --turn` / the Docker Go stage; if it's absent this is a silent no-op.
- `turn_shared_secret` is auto-generated once (and persisted) on first start, so FastAPI's minted TURN REST
  credentials (app/routers/calls.py) and the Pion server share the same secret with zero manual config.
- Runs only on the port-3051 instance (wired in app/main.py), like the relay/bot supervisors.
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

# repo_root/turnserver/pion-turn  (app/services/turn_service.py -> parents[2] == repo root)
_TURN_BIN = Path(__file__).resolve().parents[2] / "turnserver" / "pion-turn"

_RECONCILE_INTERVAL = 5           # seconds between desired-state checks
_MAX_RESTARTS_PER_HOUR = 12       # crash-loop backstop
_RESTART_WINDOW = 3600

_lock = threading.RLock()
_proc: Optional[subprocess.Popen] = None
_monitor_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_restart_count = 0
_restart_window_start = 0.0
_gaveup_logged = False
_spawn_sig: Optional[str] = None   # config the running proc was spawned with; restart when it changes

# Settings whose change requires respawning pion-turn to take effect (env-baked at spawn time).
_SIG_KEYS = ("turn_public_ip", "turn_realm", "turn_port", "turn_relay_min_port", "turn_relay_max_port",
             "turn_tls_port", "turn_tls_cert", "turn_tls_key", "turn_shared_secret")


def _cfg_sig(cfg: dict) -> str:
    return "|".join((cfg.get(k, "") or "") for k in _SIG_KEYS)


def _cfg() -> dict:
    return settings_store.all_settings()


def _enabled(cfg: dict) -> bool:
    return (cfg.get("turn_enabled", "false") or "").strip().lower() == "true"


def _ensure_secret(cfg: dict) -> str:
    """Return the shared secret, generating + persisting one on first use so nothing needs manual setup."""
    sec = (cfg.get("turn_shared_secret", "") or "").strip()
    if sec:
        return sec
    sec = secrets.token_hex(32)
    try:
        settings_store.put("turn_shared_secret", sec)
        logger.info("[turn] generated a TURN shared secret on first start")
    except Exception as e:  # pragma: no cover - persistence best-effort
        logger.warning("[turn] could not persist generated secret: %s", e)
    return sec


def _build_env(cfg: dict) -> dict:
    env = dict(os.environ)
    # secret is resolved (+ persisted) by _reconcile before this, so cfg already carries it — keep it
    # consistent with the signature used to decide restarts.
    env["PC_TURN_SECRET"] = (cfg.get("turn_shared_secret", "") or "").strip()
    env["PC_TURN_PUBLIC_IP"] = (cfg.get("turn_public_ip", "") or "").strip()
    env["PC_TURN_REALM"] = (cfg.get("turn_realm", "") or "posterchan").strip()
    env["PC_TURN_PORT"] = (cfg.get("turn_port", "") or "3478").strip()
    env["PC_TURN_MIN_PORT"] = (cfg.get("turn_relay_min_port", "") or "49160").strip()
    env["PC_TURN_MAX_PORT"] = (cfg.get("turn_relay_max_port", "") or "49200").strip()
    tls_port = (cfg.get("turn_tls_port", "") or "").strip()
    cert = (cfg.get("turn_tls_cert", "") or "").strip()
    key = (cfg.get("turn_tls_key", "") or "").strip()
    if tls_port and cert and key:
        env["PC_TURN_TLS_PORT"] = tls_port
        env["PC_TURN_TLS_CERT"] = cert
        env["PC_TURN_TLS_KEY"] = key
    return env


def _wanted(cfg: dict) -> bool:
    """We should be running the relay when it's enabled, the binary exists, and a public IP is configured."""
    if not _enabled(cfg):
        return False
    if not _TURN_BIN.exists():
        return False
    if not (cfg.get("turn_public_ip", "") or "").strip():
        return False
    return True


def _running() -> bool:
    return _proc is not None and _proc.poll() is None


def _spawn(cfg: dict) -> None:
    global _proc
    try:
        _proc = subprocess.Popen([str(_TURN_BIN)], env=_build_env(cfg), cwd=str(_TURN_BIN.parent))
        logger.info("[turn] started pion-turn (pid %s) on port %s", _proc.pid, cfg.get("turn_port", "3478"))
    except Exception as e:
        logger.error("[turn] failed to spawn pion-turn: %s", e)
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
    """Bring the relay in line with the desired state; restart on config change; rate-limit crash restarts."""
    global _restart_count, _restart_window_start, _gaveup_logged, _spawn_sig
    with _lock:
        if _stop_event.is_set():
            return  # shutting down — never (re)spawn (avoids the terminate→respawn orphan race)
        cfg = _cfg()
        want = _wanted(cfg)
        if not want:
            if _running():
                logger.info("[turn] turn disabled/misconfigured — stopping pion-turn")
                _terminate()
            _restart_count = 0            # reset the crash cap so a later re-enable isn't wrongly parked
            _gaveup_logged = False
            _spawn_sig = None
            return
        # Resolve (and persist once) the shared secret so cfg + the spawn signature + the env all agree.
        cfg["turn_shared_secret"] = _ensure_secret(cfg)
        sig = _cfg_sig(cfg)
        if _running():
            if sig == _spawn_sig:
                return
            logger.info("[turn] TURN settings changed — restarting pion-turn to apply")
            _terminate()  # fall through to respawn with the new config
        # (re)start under the hourly crash cap
        now = time.time()
        if now - _restart_window_start > _RESTART_WINDOW:
            _restart_window_start = now
            _restart_count = 0
            _gaveup_logged = False
        if _restart_count >= _MAX_RESTARTS_PER_HOUR:
            if not _gaveup_logged:
                logger.error("[turn] pion-turn restart cap hit (%d/hr) — parking until next window",
                             _MAX_RESTARTS_PER_HOUR)
                _gaveup_logged = True
            return
        _restart_count += 1
        _spawn(cfg)
        _spawn_sig = sig


def _monitor_loop() -> None:
    # Reconcile once configuration-dependent settings are readable, then every interval until stopped.
    while not _stop_event.is_set():
        try:
            _reconcile()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("[turn] reconcile error: %s", e)
        _stop_event.wait(_RECONCILE_INTERVAL)


def start_turn_server() -> None:
    """Idempotently start the supervisor thread (which starts/stops pion-turn per settings)."""
    global _monitor_thread
    with _lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        if not _TURN_BIN.exists():
            logger.info("[turn] pion-turn binary not built (%s) — TURN relay disabled "
                        "(build it with install.sh --turn to enable calls behind NAT)", _TURN_BIN)
            return
        _stop_event.clear()
        _monitor_thread = threading.Thread(target=_monitor_loop, name="turn-monitor", daemon=True)
        _monitor_thread.start()
        logger.info("[turn] supervisor started")


def stop_turn_server() -> None:
    """Stop the supervisor + terminate the relay (kept under the ~3s service-stop deadline)."""
    global _monitor_thread
    _stop_event.set()
    with _lock:
        _terminate()
    t = _monitor_thread
    if t is not None:
        t.join(timeout=3)
    _monitor_thread = None
    logger.info("[turn] supervisor stopped")
