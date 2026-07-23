"""Per-user Debian Docker sandbox for agentic node tasks.

Non-admin AI users (`can_ai`) — and admins who opt in — run `node`/`agent` commands inside a
THROWAWAY per-user Debian container instead of on the host, so the agent can do anything *inside*
the box without ever touching the host filesystem/services. Containers are:

  * created LAZILY on the first command (one per user id, `pcai-sbx-<uid>`),
  * capped (memory / cpus / pids) and run with `--security-opt no-new-privileges`,
  * reaped when a background AGENT run finishes ("delete when the job's done"), and
  * idle-reaped by a periodic sweep so a bare `node sandbox <cmd>` session can't linger forever.

Requires Docker on the host + the service user in the `docker` group (else `available()` is False and
the sandbox is simply not offered). Execution rides the SAME `node_service` job machinery — only the
process argv differs (`docker exec` vs a host shell), so streaming / timeout / kill all work unchanged.
"""
import asyncio
import logging
import time
from typing import Optional

from app.services import settings_store

logger = logging.getLogger(__name__)

_PREFIX = "pcai-sbx-"
_last_use: dict = {}          # uid(str) -> last-use epoch, for the idle reaper
_active: dict = {}            # uid(str) -> count of in-flight execs/runs; a container in use is never reaped
_lock = asyncio.Lock()        # guards _last_use/_active + the create/start decision (per-process; single worker)
_docker_ok: Optional[bool] = None


def _s(key: str, default: str = "") -> str:
    return (settings_store.get(key) or default)


def enabled() -> bool:
    """Master toggle (Admin → Services). Off by default — the sandbox is not offered until enabled."""
    return _s("node_exec_sandbox_enabled", "false").strip().lower() == "true"


def _image() -> str:
    return _s("node_exec_sandbox_image", "debian:stable-slim").strip() or "debian:stable-slim"


def _network() -> str:
    # "bridge" lets the agent apt-install tools; "none" fully isolates. Container can't reach the host
    # either way (that's the whole point) — this only controls outbound internet from inside the box.
    return _s("node_exec_sandbox_network", "bridge").strip() or "bridge"


def _mem() -> str:
    return _s("node_exec_sandbox_memory", "1g").strip() or "1g"


def _cpus() -> str:
    return _s("node_exec_sandbox_cpus", "1").strip() or "1"


def container_name(uid) -> str:
    return f"{_PREFIX}{uid}"


async def _docker(*args, timeout: float = 60) -> tuple[int, str]:
    """Run a `docker` CLI command; return (returncode, combined output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return 127, "docker CLI not found"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return 124, f"docker {args[0] if args else ''} timed out"
    return proc.returncode, (out or b"").decode("utf-8", "replace")


async def available() -> bool:
    """True if the Docker daemon is reachable by THIS process (CLI present + socket permission). A
    SUCCESS is cached forever (docker won't vanish); a FAILURE is NOT cached — a transient probe miss
    during a slow daemon boot would otherwise disable the reaper permanently until a restart (L2)."""
    global _docker_ok
    if _docker_ok:
        return True
    rc, _ = await _docker("info", "--format", "{{.ServerVersion}}", timeout=8)
    if rc == 0:
        _docker_ok = True
    return bool(_docker_ok)


async def _running(name: str) -> bool:
    rc, out = await _docker("ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}", timeout=10)
    return rc == 0 and name in out.split()


async def _exists(name: str) -> bool:
    rc, out = await _docker("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}", timeout=10)
    return rc == 0 and name in out.split()


async def ensure(uid) -> str:
    """Create + start this user's container if it isn't already running; return its name. Called lazily
    by the first command. `_last_use` is stamped INSIDE the lock BEFORE `docker run` so the container is
    'tracked' the instant creation begins — the periodic reaper only touches tracked+idle containers now,
    but this also keeps a just-created box from ever looking idle (H2)."""
    key = str(uid)
    name = container_name(uid)
    async with _lock:
        _last_use[key] = time.time()
        if not await _running(name):
            if await _exists(name):
                await _docker("start", name, timeout=30)
            else:
                rc, out = await _docker(
                    "run", "-d", "--name", name, "--hostname", "sandbox",
                    "--memory", _mem(), "--cpus", _cpus(), "--pids-limit", "256",
                    "--network", _network(), "--security-opt", "no-new-privileges",
                    "--label", "pcai-sandbox=1",
                    _image(), "sleep", "infinity", timeout=120,
                )
                if rc != 0:
                    logger.warning("[sandbox] create failed for uid=%s: %s", uid, out.strip()[:300])
                    raise RuntimeError(f"could not start your sandbox: {out.strip()[:200]}")
                logger.info("[sandbox] created container %s", name)
    return name


async def acquire(uid) -> None:
    """Mark the container in-use (ensure + refcount++). A container with a non-zero refcount is NEVER
    reaped by the idle reaper or a polite (force=False) reap — so a long command, a slow model-thinking
    gap, or a second concurrent run for the same user can't have the box pulled out from under it (H1/M1)."""
    await ensure(uid)
    key = str(uid)
    async with _lock:
        _active[key] = _active.get(key, 0) + 1
        _last_use[key] = time.time()


async def release(uid) -> None:
    """Drop one in-use hold (refcount--) and refresh idle time (so the TTL starts from when work ENDED)."""
    key = str(uid)
    async with _lock:
        if _active.get(key, 0) > 0:
            _active[key] -= 1
        _last_use[key] = time.time()


def exec_argv(uid, command: str) -> list:
    """The argv to run `command` INSIDE this user's container (bash login shell so PATH/apt work)."""
    _last_use[str(uid)] = time.time()
    return ["docker", "exec", "-i", container_name(uid), "bash", "-lc", command]


async def reap(uid, force: bool = True) -> bool:
    """Remove this user's container. force=True (delete/cancel) removes unconditionally; force=False (end
    of an agent run, idle sweep) removes ONLY when nothing else holds it (refcount 0) — so it can't kill a
    container a concurrent run is still using (H1). Returns True if it removed the container."""
    key = str(uid)
    async with _lock:
        if not force and _active.get(key, 0) > 0:
            return False              # still in use by another exec/run
        _last_use.pop(key, None)
        _active.pop(key, None)
    rc, out = await _docker("rm", "-f", container_name(uid), timeout=30)
    if rc == 0:
        logger.info("[sandbox] reaped container for uid=%s", uid)
    return rc == 0


async def reap_idle(ttl: float = 900) -> int:
    """Reap TRACKED containers idle > ttl and NOT in use. No orphan sweep here — sweeping untracked
    containers races an in-flight `ensure`/run (a just-created or restart-survivor box mid-run looks
    'untracked'); leftovers from a PRIOR process are cleared once by `reap_all()` at startup instead."""
    now = time.time()
    async with _lock:
        due = [u for u, ts in list(_last_use.items()) if now - ts > ttl and _active.get(u, 0) == 0]
    reaped = 0
    for u in due:
        if await reap(u, force=False):   # re-checks refcount under the lock
            reaped += 1
    return reaped


async def reap_all() -> int:
    """Startup-only: remove EVERY pcai-sandbox container left by a PRIOR process. Safe because a fresh
    process has no active runs, so nothing can be reaped mid-use."""
    rc, out = await _docker("ps", "-a", "--filter", "label=pcai-sandbox=1", "--format", "{{.Names}}", timeout=10)
    reaped = 0
    if rc == 0:
        for nm in out.split():
            if nm.startswith(_PREFIX):
                await _docker("rm", "-f", nm, timeout=30)
                reaped += 1
    async with _lock:
        _last_use.clear()
        _active.clear()
    return reaped
