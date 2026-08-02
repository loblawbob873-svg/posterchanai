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
ROLES = ("all", "app", "relay", "worker", "media", "bots")

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
}


_warned = set()


def current() -> str:
    """This process's role, falling back to 'all' for anything unrecognised.

    Falling back rather than trusting the string is the difference between a typo in a unit file
    being noisy and being invisible: an unknown role matched nothing in _OWNERS, so `owns()` returned
    False for EVERY component and the process started, logged nothing unusual, and supervised
    nothing at all. `POSTERCHANAI_ROLE=relayy` would have silently taken the relay off a node while
    systemd reported the service as healthy. Now it behaves as 'all' — the safe direction, since that
    is merely the old single-process layout — and says so once."""
    raw = (os.environ.get("POSTERCHANAI_ROLE") or "").strip().lower()
    if not raw:
        return "all"
    if raw not in ROLES:
        if raw not in _warned:
            _warned.add(raw)
            logger.warning("[role] unknown POSTERCHANAI_ROLE=%r — treating this process as 'all' "
                           "(valid: %s)", raw, ", ".join(ROLES))
        return "all"
    return raw


def owns(component: str) -> bool:
    """True if this process should start (and stop) `component`.

    Unknown components default to True rather than False, deliberately: a component added later
    without a mapping keeps the pre-split behaviour of running with the app instead of silently never
    starting anywhere — a missing feature is easier to notice than a missing supervisor.
    """
    role = current()
    if role == "all":
        return True
    return role in _OWNERS.get(component, (role,))
