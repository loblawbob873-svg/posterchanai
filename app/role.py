"""Which components this process is responsible for supervising.

One helper, read by app/main.py's startup and shutdown, so "does this process own the relay?" is
answered the same way in both places. Getting that pair out of step is how you leak a subprocess: the
app starts mediamtx because it owns it, then a role check on the shutdown side disagrees and it is
never stopped.

`all` is the DEFAULT and means the historical single-process layout — the web app supervises the
relay, the worker, mediamtx, pion-turn and the bots. Every other role owns exactly one thing, so the
components can be separate systemd units and a deploy restarts only what changed.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# The single definition — run.py imports this for its --role choices, so the CLI, the unit files and
# this predicate can never disagree about what a valid role is.
ROLES = ("all", "app", "relay", "worker", "media", "bots", "tor", "proxy", "git", "shell")

# component -> the roles that supervise it. 'all' is implicit for every component (see owns()).
_OWNERS = {
    "relay":  ("relay",),
    "worker": ("worker",),
    "media":  ("media",),    # mediamtx + pion-turn
    "bots":   ("bots",),
    # In-process schedulers with no separate unit (reminders, markets, catalog refresh, blossom
    # cleanup, git host, stream-end reaper). They live with the web app because they need its loop or
    # its websocket push, so 'app' owns them.
    "app":    ("app",),
    # Externally visible, slow to re-establish, and independently restartable:
    #   tor    — circuits take minutes to rebuild and the .onion goes with them
    #   proxy  — the HTTP proxy fronting tor (its consumers reach it over TCP, so it is already
    #            cross-process; splitting only decouples its restart)
    #   git    — a restart kills in-flight clones and pushes
    "tor":    ("tor",),
    "proxy":  ("proxy",),
    "git":    ("git",),
    # The SSH terminal keeper. Split out for the opposite reason to everything else here: not because
    # a restart of IT is expensive, but because a restart of the APP must not touch it. A shell that
    # dies on every `./sync.sh` is a shell you cannot leave anything running in, and this app is
    # deployed several times a day. Under `all` it runs in-process, which works and simply does not
    # outlive a deploy.
    "shell":  ("shell",),
}


_warned = set()


def current() -> str:
    """This process's role string (may be comma-separated), falling back to 'all'.

    Falling back rather than trusting the string is the difference between a typo in a unit file
    being noisy and being invisible: an unknown role matched nothing in _OWNERS, so `owns()` returned
    False for EVERY component and the process started, logged nothing unusual, and supervised
    nothing at all. `POSTERCHANAI_ROLE=relayy` would have silently taken the relay off a node while
    systemd reported the service as healthy. Now it behaves as 'all' — the safe direction, since that
    is merely the old single-process layout — and says so once."""
    raw = (os.environ.get("POSTERCHANAI_ROLE") or "").strip().lower()
    if not raw:
        return "all"
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts or any(p not in ROLES for p in parts):
        if raw not in _warned:
            _warned.add(raw)
            logger.warning("[role] unknown POSTERCHANAI_ROLE=%r — treating this process as 'all' "
                           "(valid: %s, comma-separated)", raw, ", ".join(ROLES))
        return "all"
    return ",".join(parts)


def roles() -> set:
    """The set of roles this process fills. COMMA-SEPARATED is supported because the split is not
    all-or-nothing: some components genuinely have to stay with the web app. The bot manager is the
    worked example — Admin -> Bots reads its live process registry (`_procs`) and drives start/stop/
    publish through it, and that registry is IN-PROCESS. Run the manager elsewhere and the admin UI
    shows every bot as stopped (they are running fine) while `reconcile_now()` from a button press
    makes the APP spawn a second copy of every bot. So that node runs `--role app,bots`."""
    cur = current()
    if cur == "all":
        return set(ROLES)
    return set(cur.split(","))


def owns(component: str) -> bool:
    """True if this process should start (and stop) `component`.

    Unknown components default to True rather than False, deliberately: a component added later
    without a mapping keeps the pre-split behaviour of running with the app instead of silently never
    starting anywhere — a missing feature is easier to notice than a missing supervisor.
    """
    mine = roles()
    if "all" in mine or mine == set(ROLES):
        return True
    owners = _OWNERS.get(component)
    if owners is None:
        return True          # unmapped component: keep the pre-split behaviour (runs with the app)
    return bool(mine.intersection(owners))


def restart_owner_process(status_path: str, marker: str) -> dict:
    """Ask the process that OWNS a component to exit, so its unit restarts it with fresh config.

    Used by control paths reachable from the WEB APP for components the app no longer supervises —
    an admin Settings save that "restarts the relay" or "reconciles the git host". Left unguarded,
    those call the component's own start_*(), which spawns a SECOND copy as a child of the app: two
    relays on one Postgres, two git hosts on one port. The newcomer crash-loops on the bound port, so
    it is loud rather than silent, but it is still wrong.

    `status_path` is the component's status file (it already carries a live pid — that is how the
    admin UI reports liveness cross-process). `marker` must appear in the target's cmdline.

    The pid is VERIFIED before signalling: it must still exist, its cmdline must name this repo AND
    contain `marker`. A stale status file whose pid has been recycled would otherwise SIGTERM an
    unrelated process — including, if the marker were loose enough, the web app itself.
    """
    import json as _json
    import signal as _signal
    try:
        with open(status_path) as f:
            pid = int(_json.load(f).get("pid") or 0)
    except Exception as e:
        return {"ok": False, "error": f"owner status file unreadable ({e}); restart the unit by hand"}
    if pid <= 0:
        return {"ok": False, "error": "no owner pid recorded; restart the unit by hand"}
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return {"ok": False, "error": f"pid {pid} is not running; systemd should respawn it"}
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo not in cmd or marker not in cmd:
        logger.warning("[role] refusing to signal pid %s — cmdline is not %r of this repo: %s",
                       pid, marker, cmd[:120])
        return {"ok": False, "error": "could not identify the owning process; restart the unit by hand"}
    try:
        os.kill(pid, _signal.SIGTERM)
        logger.info("[role] asked the %s owner (pid %s) to restart for a config change", marker, pid)
        return {"ok": True, "restarted": True, "via": "service"}
    except OSError as e:
        return {"ok": False, "error": f"could not signal pid {pid}: {e}"}


def restart_owner_by_cmdline(marker: str) -> dict:
    """Same as restart_owner_process, for a component with NO status file: find the owning process by
    scanning /proc for a cmdline that names this repo and `marker`.

    Tor is the case. Its live onion toggle (set_onion) SIGHUPs a process handle the app no longer
    has, so from the web app it silently did nothing — the admin toggle appeared to work and the
    .onion never changed. Signalling the tor unit instead costs a circuit rebuild rather than a
    SIGHUP reload, which is the honest trade for a toggle that actually takes effect: the settings
    are already persisted by the time this is called, so the restarted daemon reads the new config.
    """
    import signal as _signal
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    me = os.getpid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if repo in cmd and marker in cmd:
            try:
                os.kill(pid, _signal.SIGTERM)
                logger.info("[role] asked the %s owner (pid %s) to restart for a config change", marker, pid)
                return {"ok": True, "restarted": True, "via": "service", "pid": pid}
            except OSError as e:
                return {"ok": False, "error": f"could not signal pid {pid}: {e}"}
    return {"ok": False, "error": f"no running process matching {marker!r} in this repo; restart the unit by hand"}
