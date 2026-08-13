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
import os
import time
from typing import Optional

from app.services import settings_store

logger = logging.getLogger(__name__)

_PREFIX = "pcai-sbx-"
_last_use: dict = {}          # uid(str) -> last-use epoch, for the idle reaper
_active: dict = {}            # uid(str) -> count of in-flight execs/runs; a container in use is never reaped
_locks: dict = {}             # uid(str) -> asyncio.Lock, serializing create/start/reap for THAT uid only —
                              # so one user's container build can't head-of-line-block another user (B3).
_docker_ok: Optional[bool] = None
# NB: _last_use / _active are mutated with single, await-free statements, which are atomic under asyncio's
# single-threaded loop — so they need no lock; the per-uid lock only serializes the docker create/reap
# (which have awaits). reap_idle snapshots them atomically, then reap() re-checks the refcount under the lock.


def _lock_for(uid) -> asyncio.Lock:
    key = str(uid)
    lk = _locks.get(key)
    if lk is None:
        lk = asyncio.Lock()
        _locks[key] = lk
    return lk


def _s(key: str, default: str = "") -> str:
    return (settings_store.get(key) or default)


def enabled() -> bool:
    """Master toggle (Admin → Nodes). Off by default — the sandbox is not offered until enabled."""
    return _s("node_exec_sandbox_enabled", "false").strip().lower() == "true"


def agent_node_name() -> str:
    """The single node ALL sandbox/agentic runs are pinned to (Admin → Nodes → 'Agent node'). Empty =
    run on THIS host. When it names a full peer node, the whole run (container + agent loop) is dispatched
    there over the existing Nostr transport, and that worker's own 1-at-a-time agent lock QUEUES concurrent
    runs — so we reuse the proven transport + queue instead of a bespoke placement/LB scheme. This replaced
    the old deterministic sha256(uid)%nodes container load-balancer (too much coordination-free machinery
    for what is really just 'send agentic work to one node')."""
    return _s("node_exec_agent_node", "").strip()


# The default sandbox image is now BUILT from Dockerfile.sandbox (python:3.12-slim + bech32/coincurve/
# websockets/requests), not a bare registry image. Bump this tag whenever Dockerfile.sandbox changes so a
# node rebuilds instead of reusing a stale layer. A registry image name in the setting still works — this
# is only the default.
_DEFAULT_IMAGE = "posterchanai-sandbox:3"


def _image() -> str:
    return _s("node_exec_sandbox_image", _DEFAULT_IMAGE).strip() or _DEFAULT_IMAGE


def _is_builtin_image(name: str) -> bool:
    """Our locally-BUILT image (vs a registry image the daemon can pull). Only this one is auto-built."""
    return name.split(":", 1)[0] == _DEFAULT_IMAGE.split(":", 1)[0]


def _dockerfile_path() -> str:
    # sandbox_service.py -> app/services/ -> app/ -> repo root, where Dockerfile.sandbox ships.
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "Dockerfile.sandbox")


async def _image_present(name: str) -> bool:
    rc, _ = await _docker("image", "inspect", name, timeout=15)
    return rc == 0


async def _ensure_image() -> None:
    """Build the built-in sandbox image from Dockerfile.sandbox if it isn't present yet. Unlike a
    registry image, a locally-built one can't be lazily PULLED — so without this a node that skipped the
    installer's build step would hard-fail every sandbox run. A registry image (custom setting) is left
    to `docker run` to pull. Serialized so two concurrent first-runs don't build twice."""
    name = _image()
    if not _is_builtin_image(name) or await _image_present(name):
        return
    dockerfile = _dockerfile_path()
    if not os.path.exists(dockerfile):
        logger.warning("[sandbox] %s missing and Dockerfile.sandbox not found at %s — sandbox will fail",
                       name, dockerfile)
        return
    async with _lock_for("__image_build__"):
        if await _image_present(name):          # another task built it while we waited on the lock
            return
        logger.info("[sandbox] building %s from %s (first use)…", name, dockerfile)
        rc, out = await _docker("build", "-t", name, "-f", dockerfile,
                                os.path.dirname(dockerfile), timeout=600)
        if rc != 0:
            logger.warning("[sandbox] build of %s failed: %s", name, out.strip()[:400])
        else:
            logger.info("[sandbox] built %s", name)


def _network() -> str:
    # "bridge" lets the agent apt-install tools; "none" fully isolates. Container can't reach the host
    # either way (that's the whole point) — this only controls outbound internet from inside the box.
    return _s("node_exec_sandbox_network", "bridge").strip() or "bridge"


def workspace_enabled() -> bool:
    """Give each user's sandbox a PERSISTENT /workspace (a named Docker volume), on by default.

    The container itself stays throwaway — it is reaped when an agent run finishes — so without this
    the agent had nowhere to keep anything: every run started on bare Debian with no checkout, no
    files, and no memory of the last one. That makes multi-run work (clone, edit, test, come back
    tomorrow) impossible. The volume outlives the container and is NOT removed by reap(); it is the
    one thing a user is meant to keep."""
    return _s("node_exec_sandbox_workspace", "true").strip().lower() == "true"


def workspace_volume(uid) -> str:
    return f"pcai-ws-{uid}"


WORKSPACE_DIR = "/workspace"


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


async def _container_image(name: str) -> str:
    """What image this container was actually CREATED from — which is not the same question as what
    `_image()` says it should be."""
    rc, out = await _docker("inspect", "-f", "{{.Config.Image}}", name, timeout=15)
    return out.strip() if rc == 0 else ""


async def _stale_image(name: str) -> bool:
    """A CONTAINER OUTLIVES THE IMAGE IT WAS MADE FROM, and nothing here used to notice.

    `ensure()` asked only whether a container of this name exists, so the first one a user ever got
    was reused for the life of that user — a box created back when the default was `debian:stable-slim`
    was still being handed out long after the default became a purpose-built image. Bumping the tag,
    or setting `node_exec_sandbox_image`, changed nothing for anybody who had already run one command,
    which is everybody. The symptom is whatever the new image added being MISSING: `git: command not
    found` in a container that is supposed to have git, with the setting plainly reading :3.

    Compared as written, not resolved through the daemon: an image reference is what `docker run` was
    given, and that is the string we want to know has changed. An unreadable answer is NOT treated as
    stale — failing to inspect must never become a reason to destroy somebody's container.
    """
    if not _is_builtin_image(_image()):
        return False                      # a registry image can be re-pulled/re-tagged; not our call
    was = await _container_image(name)
    return bool(was) and was != _image()


async def ensure(uid) -> str:
    """Create + start this user's container if it isn't already running; return its name. Called lazily
    by the first command. `_last_use` is stamped BEFORE `docker run` so the container is 'tracked' the
    instant creation begins (H2). The per-uid lock serializes concurrent creates for the SAME uid without
    blocking other users."""
    key = str(uid)
    name = container_name(uid)
    _last_use[key] = time.time()          # atomic; track immediately so the reaper/orphan-sweep never grabs it
    async with _lock_for(uid):
        # Replace a container built from a DIFFERENT image before deciding it is reusable. Only while
        # nothing is running in it — a refcount above zero means somebody's command is mid-flight, and
        # pulling the box out from under it is worse than one more run on the old image (the next
        # idle run picks it up). The workspace is a named volume, so a replaced container comes back
        # to the same files.
        if int(_active.get(key, 0)) == 0 and await _exists(name) and await _stale_image(name):
            logger.info("[sandbox] %s was built from %r, image is now %r — recreating",
                        name, await _container_image(name), _image())
            await _docker("rm", "-f", name, timeout=60)
        if not await _running(name):
            if await _exists(name):
                await _docker("start", name, timeout=30)
            else:
                await _ensure_image()   # build the built-in image on first use if a node lacks it
                # The persistent workspace is mounted (and made the working dir) at create time, so a
                # container recreated after a reap comes back to the SAME files. Docker creates the
                # named volume on first use — no separate `volume create` step.
                _ws = (["-v", f"{workspace_volume(key)}:{WORKSPACE_DIR}", "-w", WORKSPACE_DIR]
                       if workspace_enabled() else [])
                rc, out = await _docker(
                    "run", "-d", "--name", name, "--hostname", "sandbox",
                    "--memory", _mem(), "--cpus", _cpus(), "--pids-limit", "256",
                    "--network", _network(), "--security-opt", "no-new-privileges",
                    "--label", "pcai-sandbox=1", *_ws,
                    _image(), "sleep", "infinity", timeout=120,
                )
                if rc != 0:
                    logger.warning("[sandbox] create failed for uid=%s: %s", uid, out.strip()[:300])
                    raise RuntimeError(f"could not start your sandbox: {out.strip()[:200]}")
                logger.info("[sandbox] created container %s", name)
    return name


async def acquire(uid) -> None:
    """Mark the container in-use (ensure + refcount++). A container with a non-zero refcount is NEVER
    reaped by the idle reaper or a polite (force=False) reap — so a long command, or a second concurrent
    run for the same user, can't have the box pulled out from under it (H1/M1)."""
    await ensure(uid)
    key = str(uid)
    _active[key] = _active.get(key, 0) + 1   # atomic (no await between get + set)
    _last_use[key] = time.time()


async def release(uid) -> None:
    """Drop one in-use hold (refcount--) and refresh idle time (so the TTL starts from when work ENDED)."""
    key = str(uid)
    if _active.get(key, 0) > 0:
        _active[key] -= 1                    # atomic
    _last_use[key] = time.time()


def exec_argv(uid, command: str) -> list:
    """The argv to run `command` INSIDE this user's container (bash login shell so PATH/apt work)."""
    _last_use[str(uid)] = time.time()
    return ["docker", "exec", "-i", container_name(uid), "bash", "-lc", command]


async def reap(uid, force: bool = True) -> bool:
    """Remove this user's container. force=True (explicit delete of the LAST/only run) removes it even at a
    non-zero refcount; force=False (end of an agent run, idle sweep, delete of ONE of several concurrent
    same-user runs) removes ONLY when nothing else holds it (refcount 0) — so it can't kill a container a
    concurrent run is still using (H1/B1). Returns True if it removed the container."""
    key = str(uid)
    async with _lock_for(uid):
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
    containers races an in-flight `ensure`/run; leftovers from a PRIOR process are cleared once by
    `reap_all()` at startup instead."""
    now = time.time()
    due = [u for u, ts in list(_last_use.items()) if now - ts > ttl and _active.get(u, 0) == 0]  # atomic snapshot
    reaped = 0
    for u in due:
        if await reap(u, force=False):   # re-checks refcount under the per-uid lock
            reaped += 1
    return reaped


async def reap_all() -> int:
    """Startup: remove pcai-sandbox containers left by a PRIOR process — but ONLY UNTRACKED ones, so a
    sandbox command that arrives during the startup window (its `ensure` already tracked in _last_use) is
    never removed, and its bookkeeping is never wiped (B2). No `.clear()`: a fresh process starts empty."""
    rc, out = await _docker("ps", "-a", "--filter", "label=pcai-sandbox=1", "--format", "{{.Names}}", timeout=10)
    reaped = 0
    if rc == 0:
        tracked = {container_name(u) for u in (set(_last_use) | set(_active))}
        for nm in out.split():
            if nm.startswith(_PREFIX) and nm not in tracked:
                await _docker("rm", "-f", nm, timeout=30)
                reaped += 1
    return reaped
