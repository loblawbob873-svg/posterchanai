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
from app.services import caldav_store
from app.services.caldav import auth as caldav_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/calendar", tags=["calendar"])


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
        UserSetting.key == caldav_auth.SETTING_KEY).first())
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
                                       UserSetting.key == caldav_auth.SETTING_KEY).first()
    if not row:
        row = UserSetting(user_id=current_user.id, key=caldav_auth.SETTING_KEY, value="")
        db.add(row)
    row.value = caldav_auth.hash_password(pw)
    db.commit()
    return {"password": pw}


@router.delete("/password")
async def clear_password(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Revoke: every device syncing with the old password stops immediately."""
    db.query(UserSetting).filter(UserSetting.user_id == current_user.id,
                                 UserSetting.key == caldav_auth.SETTING_KEY).delete()
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
    existing = {c.get("id") for c in await caldav_store.list_calendars(db, current_user)}
    if cid in existing:
        base, n = cid, 2
        while f"{base}-{n}" in existing:
            n += 1
        cid = f"{base}-{n}"
    meta = {"displayname": body.name.strip() or cid, "color": body.color or ""}
    if not await caldav_store.put_calendar(db, current_user, cid, meta):
        raise HTTPException(status_code=502, detail="Could not save the calendar.")
    return {"id": cid, **meta}


@router.delete("/calendars/{cal_id}")
async def delete_calendar(cal_id: str, current_user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    _require_enabled()
    n = await caldav_store.delete_calendar(db, current_user, cal_id)
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
    return {"ok": True, "uid": uid}


@router.delete("/items")
async def delete_item(cal: str = Query(...), uid: str = Query(...),
                      current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_enabled()
    return {"ok": await caldav_store.delete_item(db, current_user, cal, uid)}


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
    raw = (await file.read())[:20_000_000]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")

    cid = _slug(cal or (file.filename or "imported").rsplit(".", 1)[0])
    known = {c.get("id") for c in await caldav_store.list_calendars(db, current_user)}
    if cid not in known:
        await caldav_store.put_calendar(db, current_user, cid,
                                        {"displayname": cal or (file.filename or "Imported").rsplit(".", 1)[0]})

    added, skipped = 0, 0
    for comp in caldav_store.split_ics(text):
        uid = caldav_store.uid_of(comp)
        if not uid:
            skipped += 1        # a component with no UID is not addressable by CalDAV
            continue
        body = caldav_store.wrap_ics([comp], cid)
        if await caldav_store.put_item(db, current_user, cid, uid, body,
                                       caldav_store.component_of(comp)):
            added += 1
        else:
            skipped += 1
    return {"ok": True, "calendar": cid, "imported": added, "skipped": skipped}
