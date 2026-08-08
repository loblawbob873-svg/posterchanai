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

NS_ITEM = "pcai:cal:"        # pcai:cal:<collection>:<uid>
NS_META = "pcai:calmeta:"    # pcai:calmeta:<collection>

# A collection is a calendar or an ADDRESSBOOK, and the difference is one field on the metadata
# document. ONE namespace for both on purpose: Radicale serves CalDAV and CardDAV from the same
# collection root, and the storage plugin hydrates a user's whole root in a single pass — a second
# namespace would mean a second relay scan on every request and two code paths to keep in step.
KIND_CALENDAR = "VCALENDAR"
KIND_ADDRESSBOOK = "VADDRESSBOOK"


def kind_of(meta: dict) -> str:
    """A collection's kind, defaulting to a calendar — every document written before addressbooks
    existed has no `kind` field and is one."""
    k = str((meta or {}).get("kind") or "").upper()
    return KIND_ADDRESSBOOK if k == KIND_ADDRESSBOOK else KIND_CALENDAR

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


async def list_collections(db, user, *, kind: str | None = None, strict: bool = False) -> list:
    """This user's collections — calendars, addressbooks, or (kind=None) both.

    See get_items for what `strict` is for.
    """
    sk = user_storage_seckey(db, user)
    docs = await store.list_docs(_port(db), NS_META, seckey=sk, strict=strict, limit=_SCAN_LIMIT)
    out = [v for v in docs.values() if isinstance(v, dict) and v.get("id")]
    if kind:
        out = [c for c in out if kind_of(c) == kind]
    out.sort(key=lambda c: (c.get("displayname") or c.get("id") or ""))
    return out


async def list_calendars(db, user, *, strict: bool = False) -> list:
    """This user's CALENDARS. Addressbooks share the namespace and must not leak into the calendar
    UI — a contact list rendered as a month grid is empty, which reads as data loss."""
    return await list_collections(db, user, kind=KIND_CALENDAR, strict=strict)


async def list_addressbooks(db, user, *, strict: bool = False) -> list:
    return await list_collections(db, user, kind=KIND_ADDRESSBOOK, strict=strict)


async def collection_kinds(db, user, *, strict: bool = True) -> dict:
    """{id: kind} for EVERY collection, both kinds.

    The id is the directory name under the CalDAV root, so calendars and addressbooks share one id
    space: two collections with the same id ARE one collection. Any caller deciding whether an id is
    free must ask this and not `list_calendars`/`list_addressbooks` — checking only its own kind
    finds no collision, and the metadata write then converts the other collection into this one and
    merges both sets of items.
    """
    return {c["id"]: kind_of(c) for c in await list_collections(db, user, strict=strict)
            if c.get("id")}


def free_id(wanted: str, taken) -> str:
    """`wanted`, or `wanted-2`, `wanted-3`… — the first id not already in use."""
    if wanted not in taken:
        return wanted
    n = 2
    while f"{wanted}-{n}" in taken:
        n += 1
    return f"{wanted}-{n}"


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


def _unfold(text: str) -> str:
    """iCalendar line folding: a continuation line begins with a space or tab. Detection has to run
    on the unfolded text — a long `DTSTART;TZID=…` wraps mid-parameter, and a folded `TZID=` is
    invisible to a line-by-line scan."""
    return text.replace("\r\n", "\n").replace("\n ", "").replace("\n\t", "")


def timezones_of(text: str) -> dict:
    """{TZID: VTIMEZONE block} for every timezone a file defines."""
    out = {}
    for comp in split_ics(text):
        if component_of(comp) != "VTIMEZONE":
            continue
        for line in _unfold(comp).split("\n"):
            if line.upper().startswith("TZID:"):
                tzid = line.split(":", 1)[1].strip()
                if tzid:
                    out.setdefault(tzid, comp)
                break
    return out


def tzids_in(component: str) -> set:
    """Every TZID a component REFERS to — `DTSTART;TZID=America/Denver:…`, including inside a VALARM.

    Only the part before the first colon is examined, because that is where parameters live: a
    DESCRIPTION whose text happens to contain "TZID=" is content, not a reference.
    """
    ids = set()
    for line in _unfold(component).split("\n"):
        head = line.split(":", 1)[0]
        for param in head.split(";")[1:]:
            if param.upper().startswith("TZID="):
                v = param.split("=", 1)[1].strip().strip('"')
                if v:
                    ids.add(v)
    return ids


def group_resources(text: str) -> list:
    """An .ics FILE → [(uid, component, [components])], one entry per CalDAV resource.

    Components that share a UID are ONE resource, not several. A recurring event with an edited
    occurrence is a master VEVENT plus a VEVENT carrying RECURRENCE-ID, both under the same UID;
    CalDAV addresses them together and iCalendar only makes sense when they travel together. Stored
    one-per-UID as separate documents, the last one written would silently win — the master would
    vanish and the calendar would show a single stray occurrence.

    VTIMEZONEs are excluded here: they are not resources, they are definitions the resources refer
    to, and `wrap_ics(..., timezones=…)` attaches the ones each resource actually needs.
    """
    order, groups, kinds = [], {}, {}
    for c in split_ics(text):
        if component_of(c) == "VTIMEZONE":
            continue
        uid = uid_of(c)
        if not uid:
            continue                     # not addressable as a resource
        if uid not in groups:
            groups[uid], kinds[uid] = [], component_of(c)
            order.append(uid)
        groups[uid].append(c)
    return [(uid, kinds[uid], groups[uid]) for uid in order]


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


def wrap_ics(components, name: str = "PosterChan", timezones: dict | None = None) -> str:
    """Components → one .ics file, the shape every calendar program imports.

    Anything already wrapped in its own VCALENDAR is UNWRAPPED first: a CalDAV client PUTs a whole
    calendar object per event, so exporting them verbatim produced a file with a VCALENDAR inside a
    VCALENDAR — which some programs import as one broken entry and others refuse outright.

    TIMEZONES TRAVEL WITH THE EVENTS THAT NEED THEM. `DTSTART;TZID=America/Denver:20220109T100000`
    means nothing without the matching VTIMEZONE, and a VTIMEZONE has no UID, so the import used to
    drop every one of them: measured on a real Radicale export, 568 of 697 events were stored
    referring to a definition that was no longer in the file. A strict client rejects that resource;
    a lenient one reads the time as floating and shifts the appointment by the UTC offset. Blocks are
    hoisted to the front (where RFC 5545 wants them), deduped by TZID, and filtered to the ones
    actually referenced, so an export carries each definition once instead of once per event.
    """
    parts = []
    for c in components:
        if not c or not c.strip():
            continue
        if "BEGIN:VCALENDAR" in c.upper():
            parts.extend(split_ics(c))
        else:
            parts.append(c)
    known, body_parts = dict(timezones or {}), []
    for c in parts:
        if component_of(c) == "VTIMEZONE":
            for tzid, block in timezones_of(c).items():
                known.setdefault(tzid, block)
        else:
            body_parts.append(c)
    wanted = set()
    for c in body_parts:
        wanted |= tzids_in(c)
    parts = [known[t] for t in known if t in wanted] + body_parts
    body = "\r\n".join(c.replace("\r\n", "\n").replace("\n", "\r\n").strip() for c in parts if c.strip())
    return ("BEGIN:VCALENDAR\r\n"
            "VERSION:2.0\r\n"
            "PRODID:-//PosterChan//Calendar//EN\r\n"
            "CALSCALE:GREGORIAN\r\n"
            f"X-WR-CALNAME:{name}\r\n"
            + (body + "\r\n" if body else "")
            + "END:VCALENDAR\r\n")


# ---------------------------------------------------------------------------------- vcard helpers

def split_vcards(text: str) -> list:
    """A .vcf FILE → one text block per vCard.

    Text-level and line-preserving, for the reason split_ics is: a card round-trips byte for byte,
    including the properties this app has no opinion about (PHOTO, X-ABLABEL groupings, the PRODID a
    phone stamps on). vCards do not nest, so this is a plain BEGIN/END scan — but a folded line
    (base64 PHOTO data is folded across dozens of lines) must stay attached to the card, which it
    does here because every line between BEGIN and END is kept verbatim.
    """
    out, cur, inside = [], [], False
    for line in text.replace("\r\n", "\n").split("\n"):
        s = line.strip().upper()
        if s.startswith("BEGIN:VCARD"):
            cur, inside = [line], True
            continue
        if inside:
            cur.append(line)
            if s.startswith("END:VCARD"):
                out.append("\n".join(cur))
                cur, inside = [], False
    return out


def uid_of_vcard(card: str) -> str:
    """The UID of one vCard, or "". Unfolded first: a UID is short, but a card written by a client
    that folds aggressively can still wrap it."""
    for line in _unfold(card).split("\n"):
        head = line.split(":", 1)[0].upper()
        # vCard 3.0 allows a group prefix — `item1.UID:` — and parameters.
        if head.split(";")[0].split(".")[-1] == "UID":
            return line.split(":", 1)[1].strip()
    return ""


def fn_of_vcard(card: str) -> str:
    """The formatted name, for listing a card without the client having to parse it."""
    for line in _unfold(card).split("\n"):
        head = line.split(":", 1)[0].upper()
        if head.split(";")[0].split(".")[-1] == "FN":
            return line.split(":", 1)[1].strip()
    return ""


def wrap_vcards(cards) -> str:
    """Cards → one .vcf file. Unlike iCalendar there is no envelope: a .vcf IS a concatenation, so
    wrapping them in anything would produce a file no client reads."""
    parts = []
    for c in cards:
        if not c or not c.strip():
            continue
        if "BEGIN:VCARD" in c.upper():
            parts.extend(split_vcards(c))
        else:
            parts.append(c)
    body = "\r\n".join(c.replace("\r\n", "\n").replace("\n", "\r\n").strip() for c in parts if c.strip())
    return (body + "\r\n") if body else ""


def _json(v):
    try:
        return json.dumps(v, separators=(",", ":"))
    except Exception:
        return "{}"
