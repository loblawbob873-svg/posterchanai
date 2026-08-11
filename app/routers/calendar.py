"""Calendar — the app's side of the bundled CalDAV server.

The CalDAV protocol itself is served by Radicale at /caldav (mounted in app/main.py); this router is
what the CLIENT talks to: list/create calendars, read a month, import an .ics, export one back out,
and mint the app password a phone needs.

Everything here reads and writes the same encrypted Nostr events Radicale's storage plugin does
(`caldav_store`), so the web UI and a synced phone are looking at one calendar rather than two that
drift.
"""
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import User, UserSetting
from app.services import caldav_store, caldav_subscribe

# Radicale is imported LAZILY, inside the handlers. `app.services.caldav.auth` and `.storage` import
# radicale at module level, and a node that has this code but not the library (sync.sh ships code,
# not deps) would otherwise fail to start the entire app. caldav_store needs no radicale at all,
# which is why the reads/writes above it are safe to import eagerly.
SETTING_KEY = "caldav_password"

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

# One import is one HTTP request and one relay burst; past this, ask for it in parts rather than
# holding a connection open for minutes.
_IMPORT_MAX_ITEMS = 5000


def _forget(username: str):
    """Tell the CalDAV storage layer to re-read this user from the relay. Lazy import so a node
    without radicale still serves the app (and this API's read paths)."""
    try:
        from app.services.caldav import storage as caldav_storage
        caldav_storage.forget_user(username)
    except Exception as e:
        logger.debug("[caldav] could not invalidate the disk cache: %s", e)


def _require_enabled():
    if not caldav_store.enabled():
        raise HTTPException(status_code=404, detail="The calendar is off on this node (Admin → Calendar).")


def _slug(name: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in (name or "").strip().lower()]
    out = "".join(keep).strip("-") or "calendar"
    return out[:48]


@router.get("/config")
async def calendar_config(request: Request, current_user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    """What the client needs to show the "add this to your phone" panel.

    The URL is built from the FORWARDED host: behind the reverse proxy the upstream connection is
    plain HTTP, so a URL derived from the raw request would tell someone to type `http://` into their
    phone — which then syncs a calendar in cleartext, or (more often) fails and looks broken.
    """
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "https"
    base = f"{proto}://{host}" if host else str(request.base_url).rstrip("/")
    has_pw = bool(db.query(UserSetting).filter(
        UserSetting.user_id == current_user.id,
        UserSetting.key == SETTING_KEY).first())
    return {
        "enabled": caldav_store.enabled(),
        "url": f"{base}/caldav/{current_user.username}/",
        "username": current_user.username,
        "has_password": has_pw,
    }


class PasswordOut(BaseModel):
    password: str


@router.post("/password", response_model=PasswordOut)
async def new_password(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Generate (or replace) this account's CalDAV app password.

    Returned ONCE, in the clear, and stored only as a PBKDF2 hash — there is no "show me my password
    again", by design: it is a per-device secret that lives in a phone's account settings, and the
    remedy for a lost one is a new one. Replacing it immediately invalidates every device using the
    old one, which is also how you revoke.
    """
    _require_enabled()
    pw = "-".join(secrets.token_urlsafe(6) for _ in range(3))
    row = db.query(UserSetting).filter(UserSetting.user_id == current_user.id,
                                       UserSetting.key == SETTING_KEY).first()
    if not row:
        row = UserSetting(user_id=current_user.id, key=SETTING_KEY, value="")
        db.add(row)
    from app.services.caldav import auth as caldav_auth      # lazy: see the note above
    row.value = caldav_auth.hash_password(pw)
    db.commit()
    return {"password": pw}


@router.delete("/password")
async def clear_password(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Revoke: every device syncing with the old password stops immediately."""
    db.query(UserSetting).filter(UserSetting.user_id == current_user.id,
                                 UserSetting.key == SETTING_KEY).delete()
    db.commit()
    return {"ok": True}


@router.get("/calendars")
async def list_calendars(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_enabled()
    return {"calendars": await caldav_store.list_calendars(db, current_user)}


class CalendarIn(BaseModel):
    name: str
    color: str = ""
    id: str = ""


@router.post("/calendars")
async def create_calendar(body: CalendarIn, current_user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    _require_enabled()
    cid = _slug(body.id or body.name)
    # STRICT: the collision check is a decision made from what is NOT there, and an unreachable relay
    # answers the same [] as "you have no calendars" — under which a new calendar reuses an existing
    # id, and its metadata write then overwrites that calendar and merges both sets of items.
    #
    # And across BOTH kinds: an addressbook shares this id space, so checking only calendars finds no
    # collision and the write below replaces `pcai:calmeta:<id>` without its `kind` — converting the
    # addressbook into a calendar, hiding it from Contacts, and leaving its vCards as calendar items
    # that a later "delete this calendar" would erase.
    try:
        existing = await caldav_store.collection_kinds(db, current_user)
    except Exception as e:
        logger.warning("[caldav] calendar list unreadable, refusing to create: %s", e)
        raise HTTPException(status_code=503, detail="Could not reach your calendars just now — try again.")
    cid = caldav_store.free_id(cid, existing)
    meta = {"displayname": body.name.strip() or cid, "color": body.color or "",
            "kind": caldav_store.KIND_CALENDAR}
    if not await caldav_store.put_calendar(db, current_user, cid, meta):
        raise HTTPException(status_code=502, detail="Could not save the calendar.")
    _forget(current_user.username)   # so a phone sees it without waiting for a restart
    return {"id": cid, **meta}


class SubscribeIn(BaseModel):
    url: str
    name: str = ""
    # Set only after the person has been shown the certificate problem and chosen to go ahead. It
    # skips certificate verification for THIS feed and nothing else; the SSRF guard is unaffected.
    insecure: bool = False


@router.post("/subscribe")
async def subscribe(body: SubscribeIn, current_user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    """Subscribe to a published .ics — a school term, a fixture list, a holiday feed.

    A subscription is a MIRROR, not an import: the refresh below also deletes what the feed has
    dropped, and the calendar is marked read-only in the UI because the next refresh would overwrite
    an edit anyway. See app/services/caldav_subscribe.py.

    The first fetch happens HERE, synchronously, and its failure is the answer — a subscription that
    is created and then silently never works is the failure mode of every feed reader ever written.
    """
    _require_enabled()
    url = caldav_subscribe.normalize_url(body.url)
    try:
        text, etag, _ = await caldav_subscribe.fetch_ics(url, insecure=body.insecure)
    except caldav_subscribe.CertificateProblem as e:
        # 400 with a MACHINE-READABLE marker, so the client can offer "subscribe anyway" instead of
        # dead-ending. A publisher's stale certificate chain is not something the reader can fix, and
        # it is common enough that treating it like a typo makes the feature look broken.
        raise HTTPException(status_code=400,
                            detail={"error": str(e), "certificate": True, "url": url})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # The feed's own name, else something readable from its host — never the generated id, which
    # would put "www-canoncityschools-org" in the sidebar as the calendar's name.
    name = ((body.name or "").strip() or caldav_subscribe.name_in(text)
            or caldav_subscribe.host_label(url).replace("-", " ").title())
    try:
        known = await caldav_store.collection_kinds(db, current_user)
    except Exception as e:
        logger.warning("[calsub] calendar list unreadable, refusing to subscribe: %s", e)
        raise HTTPException(status_code=503, detail="Could not reach your calendars just now — try again.")
    cid = caldav_store.free_id(_slug(caldav_subscribe.id_for(url, name)), known)
    meta = {"displayname": name or cid, "color": "", "kind": caldav_store.KIND_CALENDAR,
            "subscribe": {"url": url, "etag": "", "refreshed": 0, "checked": 0, "error": "",
                          "insecure": bool(body.insecure)}}
    if not await caldav_store.put_calendar(db, current_user, cid, meta):
        raise HTTPException(status_code=502, detail="Could not create the calendar.")
    res = await caldav_subscribe.refresh(db, current_user, cid, meta)
    _forget(current_user.username)
    return {"ok": True, "id": cid, "name": meta["displayname"], **res}


@router.post("/subscribe/refresh")
async def refresh_subscriptions(cal: str = Query(""), current_user: User = Depends(get_current_user),
                                db: Session = Depends(get_db)):
    """Re-fetch one subscription, or every one that is due.

    The worker refreshes these on its own schedule so a PHONE syncing over CalDAV sees new events
    without anybody opening the app; this is the "check now" button, and the on-open path for a node
    running no worker.
    """
    _require_enabled()
    cals = await caldav_store.list_calendars(db, current_user)
    out = []
    for c in cals:
        sub = caldav_subscribe.subscription_of(c)
        if not sub:
            continue
        if cal and c.get("id") != cal:
            continue
        if not cal and not caldav_subscribe.due(sub):
            continue
        out.append({"id": c.get("id"),
                    **await caldav_subscribe.refresh(db, current_user, c.get("id"), c)})
    if out:
        _forget(current_user.username)
    return {"ok": True, "refreshed": out}


@router.post("/unsubscribe")
async def unsubscribe(cal: str = Query(...), current_user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    """Stop following the feed and KEEP what it last gave us.

    Deliberately not a delete: the events are already in your phone, and turning "stop updating this"
    into "erase everything it ever gave me" is a surprise nobody wants. Deleting the calendar is a
    separate button that already exists and says what it does.
    """
    _require_enabled()
    cals = {c.get("id"): c for c in await caldav_store.list_calendars(db, current_user)}
    meta = cals.get(cal)
    if not meta:
        raise HTTPException(status_code=404, detail="No such calendar.")
    m = {k: v for k, v in meta.items() if k not in ("subscribe", "id")}
    if not await caldav_store.put_calendar(db, current_user, cal, m):
        raise HTTPException(status_code=502, detail="Could not save the calendar.")
    _forget(current_user.username)
    return {"ok": True}


@router.delete("/calendars/{cal_id}")
async def delete_calendar(cal_id: str, current_user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    _require_enabled()
    n = await caldav_store.delete_calendar(db, current_user, cal_id)
    _forget(current_user.username)
    return {"ok": True, "deleted": n}


@router.get("/items")
async def list_items(cal: str = Query(...), current_user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """Raw items for one calendar. Parsing into a month grid is the client's job — it already has to
    do that for the events it renders, and doing it twice is how two views disagree."""
    _require_enabled()
    return {"items": await caldav_store.get_items(db, current_user, cal)}


class ItemIn(BaseModel):
    cal: str
    uid: str = ""
    ics: str


@router.put("/items")
async def put_item(body: ItemIn, current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    _require_enabled()
    uid = body.uid or caldav_store.uid_of(body.ics) or secrets.token_hex(8)
    comp = caldav_store.component_of(body.ics)
    if not await caldav_store.put_item(db, current_user, body.cal, uid, body.ics, comp):
        raise HTTPException(status_code=502, detail="Could not save the event.")
    _forget(current_user.username)
    return {"ok": True, "uid": uid}


@router.delete("/items")
async def delete_item(cal: str = Query(...), uid: str = Query(...),
                      current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_enabled()
    out = await caldav_store.delete_item(db, current_user, cal, uid)
    _forget(current_user.username)
    return {"ok": out}


@router.get("/export", response_class=PlainTextResponse)
async def export_ics(cal: str = Query(...), current_user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """One calendar as a standard .ics file — the thing every other calendar program imports.

    Deliberately the SAME format Radicale exports, so moving off this node is a file copy rather than
    a migration.
    """
    _require_enabled()
    items = await caldav_store.get_items(db, current_user, cal)
    cals = {c.get("id"): c for c in await caldav_store.list_calendars(db, current_user)}
    name = (cals.get(cal) or {}).get("displayname") or cal
    body = caldav_store.wrap_ics([i.get("ics", "") for i in items], name)
    return PlainTextResponse(body, media_type="text/calendar; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{_slug(name)}.ics"'})


@router.post("/import")
async def import_ics(cal: str = Query(""), file: UploadFile = File(...),
                     current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Import an .ics export — from Radicale, Google, Thunderbird, anything.

    Existing UIDs are UPDATED rather than duplicated, so re-importing a file you have already
    imported (or a newer export of the same calendar) converges instead of doubling every
    appointment. That is the same rule the Joplin import follows, and for the same reason: imports of
    thousands of items get interrupted and re-run.
    """
    _require_enabled()
    # Read in CHUNKS against the cap rather than slicing an already-buffered body: the whole upload
    # would otherwise sit in RAM on the single worker before the limit applied. Over the cap is an
    # ERROR, not a silent truncation — a file cut mid-VEVENT imports a malformed last event.
    MAX = 20_000_000
    chunks, total = [], 0
    while True:
        chunk = await file.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX:
            raise HTTPException(status_code=413,
                                detail="That .ics is larger than 20 MB — split it and import the parts.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")

    cid = _slug(cal or (file.filename or "imported").rsplit(".", 1)[0])
    try:
        known = await caldav_store.collection_kinds(db, current_user)
    except Exception as e:
        logger.warning("[caldav] calendar list unreadable, refusing to import: %s", e)
        raise HTTPException(status_code=503, detail="Could not reach your calendars just now — try again.")
    # Importing INTO an existing calendar is the point (a re-import converges). Importing into an
    # id that belongs to an ADDRESSBOOK is not: the events would be stored among someone's contacts
    # under a collection the calendar UI never lists. Take the next free id instead.
    if known.get(cid) == caldav_store.KIND_ADDRESSBOOK:
        cid = caldav_store.free_id(cid, known)
    if cid not in known:
        # A failure here is fatal to the import, not a warning: without the calendar document the
        # items land under an id that list_calendars never returns, so they exist on the relay and
        # appear nowhere at all.
        if not await caldav_store.put_calendar(
                db, current_user, cid,
                {"displayname": cal or (file.filename or "Imported").rsplit(".", 1)[0],
                 "kind": caldav_store.KIND_CALENDAR}):
            raise HTTPException(status_code=502, detail="Could not create the calendar for this import.")

    # Whole RESOURCES, not loose components: a UID's recurrence overrides belong in one item, and the
    # VTIMEZONEs an event refers to have to be stored with it (see caldav_store.wrap_ics).
    resources = caldav_store.group_resources(text)
    tzs = caldav_store.timezones_of(text)
    if len(resources) > _IMPORT_MAX_ITEMS:
        raise HTTPException(status_code=413,
                            detail=f"That file holds {len(resources)} items; import up to {_IMPORT_MAX_ITEMS} at a time.")

    # CONCURRENTLY, in bounded batches. Each put is its own relay websocket plus a pure-Python
    # signature, so a few thousand events done one after another is minutes of stalled single-worker
    # process and an HTTP timeout for the person importing.
    import asyncio as _asyncio
    sem = _asyncio.Semaphore(8)

    async def _one(res):
        uid, comp, parts = res
        body = caldav_store.wrap_ics(parts, cid, timezones=tzs)
        async with sem:
            return await caldav_store.put_item(db, current_user, cid, uid, body, comp)

    results = await _asyncio.gather(*[_one(r) for r in resources], return_exceptions=True)
    added = sum(1 for r in results if r is True)
    skipped = len(results) - added
    _forget(current_user.username)
    return {"ok": True, "calendar": cid, "imported": added, "skipped": skipped}
