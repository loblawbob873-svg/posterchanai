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
_lock = asyncio.Lock()        # serialize create/start/reap (per-process; the app is single-worker)
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
    """True if the Docker daemon is reachable by THIS process (CLI present + socket permission).
    Cached after the first probe — a failure means the sandbox is silently not offered."""
    global _docker_ok
    if _docker_ok is not None:
        return _docker_ok
    rc, _ = await _docker("info", "--format", "{{.ServerVersion}}", timeout=8)
    _docker_ok = (rc == 0)
    if not _docker_ok:
        logger.info("[sandbox] docker not available to this process — sandbox disabled")
    return _docker_ok


async def _running(name: str) -> bool:
    rc, out = await _docker("ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}", timeout=10)
    return rc == 0 and name in out.split()


async def _exists(name: str) -> bool:
    rc, out = await _docker("ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.Names}}", timeout=10)
    return rc == 0 and name in out.split()


async def ensure(uid) -> str:
    """Create + start this user's container if it isn't already running; return its name. Called lazily
    by the first command so a user who never runs anything never spawns a container."""
    name = container_name(uid)
    async with _lock:
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
    _last_use[str(uid)] = time.time()
    return name


def exec_argv(uid, command: str) -> list:
    """The argv to run `command` INSIDE this user's container (bash login shell so PATH/apt work)."""
    _last_use[str(uid)] = time.time()
    return ["docker", "exec", "-i", container_name(uid), "bash", "-lc", command]


async def reap(uid) -> None:
    """Force-remove this user's container (called when an agent run finishes)."""
    _last_use.pop(str(uid), None)
    rc, out = await _docker("rm", "-f", container_name(uid), timeout=30)
    if rc == 0:
        logger.info("[sandbox] reaped container for uid=%s", uid)


async def reap_idle(ttl: float = 900) -> int:
    """Remove containers whose last command was more than `ttl` seconds ago. Returns the count reaped.
    Also sweeps any orphaned pcai-sandbox containers this process forgot about (e.g. after a restart)."""
    now = time.time()
    reaped = 0
    for uid, ts in list(_last_use.items()):
        if now - ts > ttl:
            await reap(uid)
            reaped += 1
    # Orphan sweep: containers labelled ours but not tracked in _last_use (survived a restart).
    rc, out = await _docker("ps", "-a", "--filter", "label=pcai-sandbox=1", "--format", "{{.Names}}", timeout=10)
    if rc == 0:
        tracked = {container_name(u) for u in _last_use}
        for nm in out.split():
            if nm.startswith(_PREFIX) and nm not in tracked:
                await _docker("rm", "-f", nm, timeout=30)
                reaped += 1
    return reaped
