"""Which notifications a DEVICE actually wants — the push watcher's half of the app's toggles.

The preferences the app shows live in a kind-30078 `pcai:client-prefs` document that is
NIP-44-encrypted to the user's OWN key, so THIS NODE CANNOT READ THEM. Without a mirror the push
watcher has no choice but to send everything, which is exactly what it did: the in-app gate
(`notificationAllowed` in app.js) only ever governed notifications the open client raised for
itself, so a closed phone still buzzed for every like, repost, zap and reply no matter what the
Notifications tab said.

So the client — the only party that can read that document — mirrors the booleans onto its own
PushSubscription rows (POST /api/push/prefs). Per ROW, not per account: a phone and a laptop are
allowed to want different things, and that is the unit the user is actually configuring.

**FAIL OPEN.** Unset, unparseable, or an unrecognised type all mean SEND. A silenced alert is
invisible by construction — nobody can tell "I turned that off" from "it was dropped" — and the
failure mode is a direct message that never arrives. Too many notifications is a complaint; a
missing one is a bug nobody can report. The default therefore has to be the noisy one.
"""
import json
import logging

logger = logging.getLogger(__name__)

# The SAME vocabulary the client's _NOTIFICATION_TYPES uses. One list of names, or a toggle labelled
# "Likes and reactions" silences something else (or nothing at all) and no test would notice.
PUSH_TYPES = ("email", "dm", "likes", "replies", "quotes", "mentions", "reposts",
              "zaps", "concord", "channels", "sms", "reminders")


def push_type(ev: dict, recipient: str = "") -> str:
    """Classify an event the way `_title` describes it, so the toggle and the wording cannot drift.

    Returns "" for anything with no matching toggle, which `allows` then treats as always-send —
    a call is the case that matters, and it must ring whatever else is switched off.
    """
    try:
        kind = int(ev.get("kind", 0))
    except Exception:
        return ""
    if kind == 9735:
        return "zaps"
    if kind == 7:
        return "likes"
    if kind == 6:
        return "reposts"
    if kind == 1111:
        return "replies"
    if kind == 42:
        return "channels"
    if kind in (4, 1059):
        return "dm"
    if kind == 1:
        # A quote is a mention of you inside somebody's own post, and _title says so first.
        try:
            from app.services.nostr_push_service import quote_pubkeys
            if recipient and recipient in quote_pubkeys(ev):
                return "quotes"
        except Exception:
            pass
        # NIP-10: an `e` tag makes it a reply to something; without one it is a plain mention.
        for t in (ev.get("tags") or []):
            if len(t) >= 2 and t[0] == "e" and t[1]:
                return "replies"
        return "mentions"
    return ""


def allows(prefs, kind_type: str) -> bool:
    """Does this subscription want `kind_type`? Anything but an explicit `false` is yes."""
    if not kind_type or kind_type not in PUSH_TYPES:
        return True                     # no toggle governs it — a call, an unknown kind
    if not prefs:
        return True                     # never configured: the old behaviour, deliberately
    try:
        data = json.loads(prefs) if isinstance(prefs, str) else prefs
    except Exception:
        return True                     # unreadable is not "they said no"
    if not isinstance(data, dict):
        return True
    return data.get(kind_type) is not False


def allows_row(sub, kind_type: str) -> bool:
    """`allows` for a subscription ROW, reading the column defensively.

    Not defensive programming for its own sake: `_poll` catches every exception at its top level, so
    an AttributeError here does not skip one device — it ABORTS THE WHOLE POLL and nobody on the
    node is notified at all, silently, for as long as the condition lasts. A row from a node that
    has not run the migration yet, or any stand-in that never had the column, would do it.

    Reading a preference must never be able to cost more than the preference itself, which is the
    same rule the fail-open default in `allows` exists for.
    """
    return allows(getattr(sub, "prefs", None), kind_type)


def clean(value) -> dict:
    """The subset of a client-supplied object worth storing: known types, booleans only.

    Mirrors `_notificationClean` in app.js. An unknown key is dropped rather than stored, so a
    renamed toggle cannot come back later as a silent permanent mute for a type nothing shows.
    """
    out = {}
    if isinstance(value, dict):
        for key in PUSH_TYPES:
            if isinstance(value.get(key), bool):
                out[key] = value[key]
    return out
