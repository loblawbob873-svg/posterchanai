"""Calendars as encrypted Nostr events — the datastore behind the bundled CalDAV server.

Shape, and why it is this one:

  * ONE event per calendar item — `pcai:cal:<calendar>:<uid>` — NIP-44-encrypted to the user's
    server-held storage key and signed by it, exactly like a chat message (`chat_store`) and a note.
    Per-item rather than one document per calendar, for the reason Notes gives: a document is a
    read-modify-write of everything on every save, so two devices editing different events lose one
    — and phones sync calendars constantly. It also makes a single event individually deletable
    (NIP-09) instead of rewriting the calendar to remove one appointment.
  * ONE event per calendar for its metadata — `pcai:calmeta:<calendar>` — the display name, colour
    and component set a CalDAV client sets with PROPPATCH.

WHAT "ENCRYPTED" MEANS HERE, precisely, because it is NOT what Notes and Budget mean. A CalDAV
client — your phone, Thunderbird — authenticates with a password and sends plain iCalendar; the
server has to read the data to answer it. So a calendar is encrypted AT REST and on the relay, with
the user's server-held storage key: the relay operator sees ciphertext, another user cannot read it,
and it is portable and replicated like every other app document. It is NOT end-to-end: this node can
read your calendar, because that is the price of your phone being able to sync with it. Notes and
Budget make the opposite trade (nobody but you can read them, and nothing syncs by CalDAV).
"""
import json
import logging
import time

from . import nostr_store as store
from .nostr_store import user_storage_seckey
from app.services import settings_store

logger = logging.getLogger(__name__)

NS_ITEM = "pcai:cal:"        # pcai:cal:<calendar>:<uid>
NS_META = "pcai:calmeta:"    # pcai:calmeta:<calendar>

# A `d`-tag PREFIX is not a thing a Nostr filter can match, so a namespace scan pulls the user's
# documents of this kind and filters here — and the keyspace is SHARED (chat_store writes one
# document per chat message with the same key and kind). At the default 5000 a heavy chat user's
# history fills the window before the calendar documents are reached, and calendars silently vanish
# from both the web UI and CalDAV. Raised here because a calendar that is half-read is worse than a
# slow read: the reconcile would treat the missing half as deleted.
_SCAN_LIMIT = 20000


def _port(db=None) -> int:
    return settings_store.get_int("nostr_relay_port", 3052)


def enabled() -> bool:
    """Is the bundled CalDAV server switched on for this node? Off by default: it opens a password
    login surface, so an operator turns it on deliberately."""
    v = settings_store.get("caldav_enabled", None)
    if v is None or str(v).strip() == "":
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------------- items

async def put_item(db, user, calendar: str, uid: str, ics: str, component: str = "VEVENT") -> bool:
    """Store one iCalendar item. `ics` is the component's own text, as the client sent it."""
    sk = user_storage_seckey(db, user)
    rec = {"cal": calendar, "uid": uid, "ics": ics, "component": component, "ts": time.time()}
    return await store.put_doc(_port(db), sk, f"{NS_ITEM}{calendar}:{uid}", rec)


async def get_items(db, user, calendar: str, *, strict: bool = False) -> list:
    """Every item in a calendar, as [{uid, ics, component, ts}].

    `strict` RAISES when the relay is unreachable instead of answering []. Any caller that decides
    something from the absence of items — the reconcile that deletes files the relay no longer has,
    the id-collision check — must use it, because [] otherwise means both "empty" and "I could not
    ask", and acting on the second deletes a calendar that is merely unreachable.
    """
    sk = user_storage_seckey(db, user)
    docs = await store.list_docs(_port(db), f"{NS_ITEM}{calendar}:", seckey=sk, strict=strict,
                                 limit=_SCAN_LIMIT)
    out = [v for v in docs.values() if isinstance(v, dict) and v.get("ics")]
    out.sort(key=lambda r: r.get("uid", ""))
    return out


async def delete_item(db, user, calendar: str, uid: str) -> bool:
    sk = user_storage_seckey(db, user)
    return await store.delete_doc(_port(db), sk, f"{NS_ITEM}{calendar}:{uid}")


# ---------------------------------------------------------------------------------- calendars

async def put_calendar(db, user, calendar: str, meta: dict) -> bool:
    """Create or update a calendar's own properties (display name, colour, component set)."""
    sk = user_storage_seckey(db, user)
    rec = dict(meta or {})
    rec["id"] = calendar
    rec.setdefault("created", time.time())
    rec["updated"] = time.time()
    return await store.put_doc(_port(db), sk, f"{NS_META}{calendar}", rec)


async def list_calendars(db, user, *, strict: bool = False) -> list:
    """This user's calendars. See get_items for what `strict` is for."""
    sk = user_storage_seckey(db, user)
    docs = await store.list_docs(_port(db), NS_META, seckey=sk, strict=strict, limit=_SCAN_LIMIT)
    cals = [v for v in docs.values() if isinstance(v, dict) and v.get("id")]
    cals.sort(key=lambda c: (c.get("displayname") or c.get("id") or ""))
    return cals


async def delete_calendar(db, user, calendar: str) -> int:
    """Drop a calendar and every item in it. Returns how many events were deleted."""
    sk = user_storage_seckey(db, user)
    port = _port(db)
    n = 0
    docs = await store.list_docs(port, f"{NS_ITEM}{calendar}:", seckey=sk, limit=_SCAN_LIMIT)
    for d in docs:
        if await store.delete_doc(port, sk, d):
            n += 1
    if await store.delete_doc(port, sk, f"{NS_META}{calendar}"):
        n += 1
    return n


# ---------------------------------------------------------------------------------- ics helpers

def split_ics(text: str) -> list:
    """Split a whole .ics FILE into its components, each wrapped as a standalone VCALENDAR.

    An export from Radicale (or Google, or Thunderbird) is one file holding every event; CalDAV
    stores one component per resource. Deliberately text-level rather than a parser dependency: the
    lines are preserved byte for byte, so an import round-trips whatever the other program wrote,
    including properties we have no opinion about.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    out, cur, depth, kind = [], [], 0, ""
    for line in lines:
        s = line.strip()
        # NESTING. A VEVENT routinely CONTAINS a VALARM (any reminder from Google, a phone or
        # Thunderbird) and a VCALENDAR can contain a VTIMEZONE with VSTANDARD/VDAYLIGHT inside it.
        # Counting every BEGIN:V… while only decrementing on an END matching the OUTER name left
        # depth stuck above zero forever, so NOTHING was ever emitted: import stored 0 events and
        # export returned a header-only file — the user's calendar looked erased, with no error.
        # So: track the component NAME on every END, and close the outer one when its own END lands.
        if s.startswith("BEGIN:V") and s != "BEGIN:VCALENDAR":
            name = s.split(":", 1)[1]
            if depth == 0:
                cur, kind = [], name
            depth += 1
            cur.append(line)
            continue
        if depth:
            cur.append(line)
            if s.startswith("END:V") and s != "END:VCALENDAR":
                depth -= 1
                if depth == 0 and s.split(":", 1)[1] == kind:
                    out.append("\n".join(cur))
                    cur, kind = [], ""
            continue
    return out


def uid_of(component: str) -> str:
    """The UID inside one component, or "" — a component without one is not addressable."""
    for line in component.replace("\r\n", "\n").split("\n"):
        if line.upper().startswith("UID:"):
            return line.split(":", 1)[1].strip()
    return ""


def component_of(component: str) -> str:
    for line in component.replace("\r\n", "\n").split("\n"):
        s = line.strip().upper()
        if s.startswith("BEGIN:V") and s != "BEGIN:VCALENDAR":
            return s.split(":", 1)[1]
    return "VEVENT"


def wrap_ics(components, name: str = "PosterChan") -> str:
    """Components → one .ics file, the shape every calendar program imports.

    Anything already wrapped in its own VCALENDAR is UNWRAPPED first: a CalDAV client PUTs a whole
    calendar object per event, so exporting them verbatim produced a file with a VCALENDAR inside a
    VCALENDAR — which some programs import as one broken entry and others refuse outright.
    """
    parts = []
    for c in components:
        if not c or not c.strip():
            continue
        if "BEGIN:VCALENDAR" in c.upper():
            parts.extend(split_ics(c))
        else:
            parts.append(c)
    body = "\r\n".join(c.replace("\r\n", "\n").replace("\n", "\r\n").strip() for c in parts if c.strip())
    return ("BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//PosterChan//Calendar//EN\r\n"
            "CALSCALE:GREGORIAN\r\n"
            f"X-WR-CALNAME:{name}\r\n"
            + (body + "\r\n" if body else "")
            + "END:VCALENDAR\r\n")


def _json(v):
    try:
        return json.dumps(v, separators=(",", ":"))
    except Exception:
        return "{}"
