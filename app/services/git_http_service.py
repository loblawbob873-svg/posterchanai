"""Supervisor for the GRASP git smart-HTTP subprocess (git_host_main.py).

A VERBATIM adaptation of the built-in Nostr relay supervisor (app/services/nostr_relay/thread.py):
a singleton Popen guarded by an RLock, a `_shutdown` flag so a deliberate stop isn't fought by the
watchdog, a ~15s daemon watchdog that respawns a crashed child with a crash-loop backoff
(>=5 crashes / 600s -> stop respawning, log loudly), and a fast terminate->wait(4s)->kill stop so a
slow child can't blow systemd's 10s stop deadline.

Everything is gated on `git_server_enabled` (default false): with it off, start_git_http() is a no-op
and NOTHING spawns — shipping this dormant is what makes a one-shot deploy safe. Wired into
app/main.py startup()/shutdown() INSIDE the `if app_port == 3051:` guard so only the main instance
runs a git host (never a second worker).

The watchdog polls lightly (sleep 15s); the child itself does all git work in its own process, so
this supervisor adds no steady CPU load.
"""

import logging
import os
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class _GitHost:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.cfg: dict = {}


_host = _GitHost()
_lock = threading.RLock()
_shutdown = False
_monitor_thread: threading.Thread | None = None
_RESPAWN_WINDOW = 600
_RESPAWN_MAX = 5
_respawn_times: list = []


def _relay_from_public_base(public_base: str) -> str:
    """`https://host/git` -> `wss://host/relay` (http -> ws). "" when the base isn't a usable URL —
    the announcement then simply carries no `relays` tag, exactly as before."""
    from urllib.parse import urlparse
    pu = urlparse((public_base or "").strip())
    if not pu.scheme or not pu.netloc:
        return ""
    return "%s://%s/relay" % ("wss" if pu.scheme == "https" else "ws", pu.netloc)


def _read_config() -> dict:
    """Read the git-host settings from the Nostr datastore (same mechanism the relay uses). The DSN
    is reused from the relay setting (same Postgres `posterchan_relay` the hook reads)."""
    from app.database import SessionLocal
    from app.services import settings_store
    db = SessionLocal()
    try:
        settings_store.load_local()
        settings_store.hydrate_from_db(db)

        def g(key, default=""):
            v = settings_store.get(key, None)
            return v if v not in (None, "") else default

        def gi(key, default):
            try:
                return int(g(key, str(default)))
            except (ValueError, TypeError):
                return default

        def gb(key, default=False):
            return str(g(key, str(default))).strip().lower() in ("1", "true", "yes", "on")

        # Proxy mode: if git_server_proxy_url is set, this node forwards smart-HTTP to the hosting
        # node instead of running its own subprocess — so the local host is DISABLED regardless of
        # git_server_enabled (see app/services/git_proxy.py + app/routers/git.py:git_smart_proxy).
        proxy_url = (g("git_server_proxy_url", "") or "").strip()
        return {
            "enabled": gb("git_server_enabled", False) and not proxy_url,
            "proxy_url": proxy_url,
            "bind": g("git_server_bind", "127.0.0.1"),
            "port": gi("git_server_port", 3053),
            "public_base": g("git_server_public_base", ""),
            # Relay to advertise in a new repo's 30617 `relays` tag. NIP-34 clients publish the
            # kind-30618 repo state THERE, so a repo announced without it is unpushable by ngit
            # ("state event failed to reach any git server relay") even though the git side is fine.
            # Explicit setting wins; otherwise derive from the public base, since this node's relay is
            # served at /relay on the same host that fronts /git (https://x/git -> wss://x/relay).
            "relay_url": g("client_relay_url", "") or _relay_from_public_base(g("git_server_public_base", "")),
            "allowlist": g("git_server_allowlist", ""),
            "repo_max_mb": gi("git_server_repo_max_mb", 512),
            "total_gb": gi("git_server_total_gb", 20),
            "allow_force": gb("git_server_allow_force", True),
            "nip98_push": gb("git_server_nip98_push", True),
            "default_private": gb("git_server_default_private", False),
            "read_skew": 300,
            "pg_dsn": g("nostr_relay_pg_dsn", os.environ.get(
                "NOSTR_RELAY_PG_DSN", "host=127.0.0.1 port=5432 dbname=posterchan_relay user=posterchan")),
        }
    finally:
        db.close()


def _spawn(cfg: dict) -> None:
    entry = os.path.join(_REPO_ROOT, "git_host_main.py")
    _host.proc = subprocess.Popen([sys.executable, entry], cwd=_REPO_ROOT)
    logger.info("[git-host] spawned subprocess pid %d (port %s)", _host.proc.pid, cfg.get("port"))


def _monitor_loop() -> None:
    """Watchdog: respawn the child if it dies, unless we're shutting down on purpose. Light poll."""
    while not _shutdown:
        time.sleep(15)
        if _shutdown:
            break
        try:
            with _lock:
                if _shutdown:
                    break
                if _host.proc is not None and _host.proc.poll() is None:
                    continue   # alive
                cfg = _host.cfg or _read_config()
                if not cfg.get("enabled"):
                    continue
                now = time.time()
                _respawn_times[:] = [t for t in _respawn_times if now - t < _RESPAWN_WINDOW]
                if len(_respawn_times) >= _RESPAWN_MAX:
                    logger.error("[git-host] crashed %d× in %dm — backing off, NOT respawning",
                                 len(_respawn_times), _RESPAWN_WINDOW // 60)
                    continue
                _respawn_times.append(now)
                logger.warning("[git-host] subprocess not running — respawning (watchdog)")
                _host.cfg = cfg
                _spawn(cfg)
        except Exception as e:
            logger.debug("[git-host] watchdog error: %s", e)


def start_git_http() -> None:
    """Idempotent. No-op unless git_server_enabled. Starts the child + the single watchdog thread."""
    global _shutdown, _monitor_thread
    with _lock:
        if _host.proc is not None and _host.proc.poll() is None:
            return
        cfg = _read_config()
        if not cfg["enabled"]:
            logger.info("[git-host] disabled (git_server_enabled off) — not starting")
            return
        _shutdown = False
        _host.cfg = cfg
        _spawn(cfg)
        if _monitor_thread is None or not _monitor_thread.is_alive():
            _monitor_thread = threading.Thread(target=_monitor_loop, name="git-host-monitor", daemon=True)
            _monitor_thread.start()


def stop_git_http() -> None:
    global _shutdown
    with _lock:
        _shutdown = True
        proc = _host.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=4)   # fast escalate so we never blow systemd's 10s stop deadline
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _host.proc = None


def restart_git_http() -> dict:
    global _shutdown
    with _lock:
        stop_git_http()
        cfg = _read_config()
        if not cfg["enabled"]:
            return {"ok": False, "error": "git server disabled"}
        _shutdown = False
        _host.cfg = cfg
        _spawn(cfg)
    return {"ok": True, "restarted": True}


def git_http_status() -> dict:
    """Liveness from the Popen handle, with a status-file fallback (pid alive + recent ts)."""
    import json
    alive = _host.proc is not None and _host.proc.poll() is None
    port = _host.cfg.get("port") if _host.cfg else None
    try:
        with open(os.path.join(_REPO_ROOT, "data", "git_http.status.json")) as f:
            st = json.load(f)
        port = st.get("port", port)
        if not alive:
            alive = (time.time() - st.get("ts", 0)) < 90 and _pid_alive(st.get("pid"))
    except (OSError, ValueError):
        pass
    return {"running": bool(alive), "port": port}


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False
