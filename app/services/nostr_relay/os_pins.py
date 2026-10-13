"""Which events the PosterChanOS local relay never evicts — the SAME rule as `_isPinned` in
static/js/client/store.js, which this relay replaces as the desktop's main event cache.

The client's cache keeps "the newest N" for the firehose and pins the user's own documents, because a
document only its author can decrypt is not refetchable the way a timeline is: evicting a note, a vault
item or the drive index means it is GONE from this machine until a relay hands it back. Moving the cache
into the relay must not quietly drop that rule, so the d-tag lists below are the client's, and
tests/test_os_relay_pins_match_the_client.py reads them back out of store.js and fails the moment the two
drift. Two pins are written by STRANGERS (mini-app announcements, notifications that p-tag the user) and
are CAPPED at the client's numbers, newest kept, so nobody can mint unevictable rows.
"""

# `t[1].startsWith('…')` in store.js _isPinned
D_PREFIXES = ("pcai:note", "pcai:pw", "pcai:sms", "pcai:files-index", "pcai:playlist")
# `t[1] === '…'` in store.js _isPinned
D_EXACT = frozenset({
    "pcai:budget", "pcai:desktop", "pcai:automute", "pcai:agent-tasks", "pcai:concord-read", "pcai:seen",
    "pcai:concord-rail", "pcai:livetemplates", "pcai:musicshares", "pcai:dmkey", "pcai:dmcache",
    "pcai:vmhosts",
})
# Pinned only when the VIEWER wrote it (a share addressed to us is anybody's to publish).
D_OWN_PREFIX = "pcai:musicshare:"

XDC_MIMES = ("application/x-webxdc", "application/webxdc+zip", "application/vnd.webxdc+zip")
XDC_PIN_MAX = 400
NOTIF_KINDS = frozenset({1, 6, 7, 9735, 1984, 1111, 1621, 1617})
NOTIF_PIN_MAX = 400


def d_pinned(d: str, author: str = "", viewer: str = "") -> bool:
    """Is a kind-30078 document with this `d` tag one of the user's pinned documents?"""
    if not isinstance(d, str):
        return False
    if d.startswith(D_PREFIXES) or d in D_EXACT:
        return True
    return bool(viewer) and d.startswith(D_OWN_PREFIX) and author == viewer


def is_pinned(ev: dict, viewer: str = "") -> bool:
    """One event, the client's predicate (the caps are applied by the caller, which can count)."""
    if not isinstance(ev, dict):
        return False
    kind = ev.get("kind")
    tags = [t for t in (ev.get("tags") or []) if isinstance(t, list) and len(t) >= 2]
    if kind in NOTIF_KINDS and viewer and ev.get("pubkey") != viewer \
            and any(t[0] == "p" and t[1] == viewer for t in tags):
        return True
    if kind == 1063:
        return any(t[0] == "m" and str(t[1]).strip().lower() in XDC_MIMES for t in tags)
    if kind != 30078:
        return False
    return any(t[0] == "d" and d_pinned(t[1], ev.get("pubkey", ""), viewer) for t in tags)
