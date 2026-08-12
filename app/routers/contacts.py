"""Contacts — the app's side of the bundled CardDAV server.

The protocol is served by the same Radicale mounted at /caldav; an addressbook is just a collection
whose kind is VADDRESSBOOK, stored by the same plugin, in the same encrypted Nostr events as
calendars (`caldav_store`). So a phone adds ONE account and gets both, and this router is only what
the web CLIENT talks to: list/create addressbooks, read and write cards, import and export .vcf.

The account, the app password and the sync URL are the calendar's — there is one CalDAV/CardDAV
identity per user, not two. `/api/calendar/config` and `/api/calendar/password` remain the single
place those live; nothing here duplicates them.

WHAT "ENCRYPTED" MEANS: exactly what it means for calendars, and it is worth repeating because
contacts feel more private than appointments. A CardDAV client sends plain vCards and the server has
to answer it, so an addressbook is encrypted at rest and on the relay with the user's server-held
storage key — the relay operator sees ciphertext and another user cannot read it, but THIS NODE CAN.
Notes and Budget make the opposite trade. See docs/CONTACTS.md.
"""
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import User
from app.services import caldav_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contacts", tags=["contacts"])

# One import is one HTTP request and one relay burst; past this, ask for it in parts.
_IMPORT_MAX_ITEMS = 5000
_IMPORT_MAX_BYTES = 20_000_000


def _forget(username: str):
    """Tell the CardDAV storage layer to re-read this user, so a phone sees a web-UI write without
    waiting for a restart. Lazy import: a node without radicale still serves this API."""
    try:
        from app.services.caldav import storage as caldav_storage
        caldav_storage.forget_user(username)
    except Exception as e:
        logger.debug("[carddav] could not invalidate the disk cache: %s", e)


def _require_enabled():
    if not caldav_store.enabled():
        raise HTTPException(status_code=404,
                            detail="Contacts are off on this node (Admin → Tools → Calendar server).")


def _slug(name: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in (name or "").strip().lower()]
    return ("".join(keep).strip("-") or "contacts")[:48]


async def _known_ids(db, user) -> dict:
    """{id: kind} for every collection this user has — calendars INCLUDED.

    Strict, and deliberately across both kinds: ids are the directory name under the CalDAV root, so
    an addressbook that reuses a calendar's id is the same collection, and its metadata write would
    convert that calendar into an addressbook and merge the two sets of items.
    """
    try:
        return await caldav_store.collection_kinds(db, user)
    except Exception as e:
        logger.warning("[carddav] collection list unreadable: %s", e)
        raise HTTPException(status_code=503, detail="Could not reach your contacts just now — try again.")


def _unreachable(what: str, e: Exception) -> HTTPException:
    """A relay we could not ask is a 503, never an empty 200.

    THE READS BEHIND THIS ROUTER DECIDE WHAT GETS DELETED FROM A PHONE. `list_docs` answers `{}` for
    both "no documents" and "I could not reach the relay" unless it is asked strictly, and a timeout
    part-way through a long read answers with the documents it managed to collect — so a flaky relay
    turns into a 200 carrying fewer contacts than the user has. The client then pushes that to the
    handset as the whole address book, and the native reconcile deletes everybody missing from it.
    That is how a real phone book emptied itself, twice. Strict here, and an error the client can see.
    """
    logger.warning("[carddav] %s unreadable: %s", what, e)
    return HTTPException(status_code=503,
                         detail="Could not reach your contacts just now — try again.")


@router.get("/books")
async def list_books(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_enabled()
    try:
        books = await caldav_store.list_addressbooks(db, current_user, strict=True)
    except HTTPException:
        raise
    except Exception as e:
        raise _unreachable("addressbook list", e)
    return {"books": books}


class BookIn(BaseModel):
    name: str
    color: str = ""
    id: str = ""


@router.post("/books")
async def create_book(body: BookIn, current_user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    _require_enabled()
    cid = caldav_store.free_id(_slug(body.id or body.name), await _known_ids(db, current_user))
    meta = {"displayname": body.name.strip() or cid, "color": body.color or "",
            "kind": caldav_store.KIND_ADDRESSBOOK}
    if not await caldav_store.put_calendar(db, current_user, cid, meta):
        raise HTTPException(status_code=502, detail="Could not save the addressbook.")
    _forget(current_user.username)
    return {"id": cid, **meta}


@router.delete("/books/{book_id}")
async def delete_book(book_id: str, current_user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    _require_enabled()
    n = await caldav_store.delete_calendar(db, current_user, book_id)
    _forget(current_user.username)
    return {"ok": True, "deleted": n}


@router.get("/cards")
async def list_cards(book: str = Query(...), current_user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """Raw cards for one addressbook. Parsed in the client, which already has to parse a vCard to
    render the editor — doing it in both places is how two views disagree.

    STRICT, for the reason spelled out in _unreachable: this list is the keep-set the phone's own
    Contacts app is reconciled against, so a short answer is not a smaller address book, it is a
    delete order."""
    _require_enabled()
    try:
        cards = await caldav_store.get_items(db, current_user, book, strict=True)
    except HTTPException:
        raise
    except Exception as e:
        raise _unreachable(f"addressbook {book!r}", e)
    return {"cards": cards}


class CardIn(BaseModel):
    book: str
    uid: str = ""
    vcf: str


@router.put("/cards")
async def put_card(body: CardIn, current_user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    _require_enabled()
    uid = body.uid or caldav_store.uid_of_vcard(body.vcf) or secrets.token_hex(8)
    if "BEGIN:VCARD" not in (body.vcf or "").upper():
        raise HTTPException(status_code=400, detail="That is not a vCard.")
    if not await caldav_store.put_item(db, current_user, body.book, uid, body.vcf, "VCARD"):
        raise HTTPException(status_code=502, detail="Could not save the contact.")
    _forget(current_user.username)
    return {"ok": True, "uid": uid}


@router.delete("/cards")
async def delete_card(book: str = Query(...), uid: str = Query(...),
                      current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _require_enabled()
    out = await caldav_store.delete_item(db, current_user, book, uid)
    _forget(current_user.username)
    return {"ok": out}


@router.get("/export", response_class=PlainTextResponse)
async def export_vcf(book: str = Query(...), current_user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """One addressbook as a standard .vcf — what every phone and mail client imports."""
    _require_enabled()
    items = await caldav_store.get_items(db, current_user, book)
    books = {c.get("id"): c for c in await caldav_store.list_addressbooks(db, current_user)}
    name = (books.get(book) or {}).get("displayname") or book
    body = caldav_store.wrap_vcards([i.get("ics", "") for i in items])
    return PlainTextResponse(body, media_type="text/vcard; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{_slug(name)}.vcf"'})


@router.post("/import")
async def import_vcf(book: str = Query(""), file: UploadFile = File(...),
                     current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Import a .vcf export — from Radicale, a phone, Google Contacts, anything.

    Existing UIDs are UPDATED rather than duplicated, so re-importing converges instead of doubling
    every contact. A card with no UID gets a generated one derived from nothing but chance, which is
    the honest answer: without a UID there is no way to tell a re-import from a new person.
    """
    _require_enabled()
    # Read in CHUNKS against the cap: the whole upload would otherwise sit in RAM on the single
    # worker. Over the cap is an ERROR, never a silent truncation — a file cut mid-card imports a
    # broken last contact.
    chunks, total = [], 0
    while True:
        chunk = await file.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        if total > _IMPORT_MAX_BYTES:
            raise HTTPException(status_code=413,
                                detail="That .vcf is larger than 20 MB — split it and import the parts.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")

    cid = _slug(book or (file.filename or "contacts").rsplit(".", 1)[0])
    known = await _known_ids(db, current_user)
    # Importing into an existing ADDRESSBOOK is the point (a re-import converges by UID). Importing
    # into an id that belongs to a CALENDAR is not: `cid in known` was true, so the metadata write
    # was skipped, and the cards were stored under a calendar — where list_addressbooks never
    # returns them and the Contacts screen shows nothing, with the import reporting success.
    if known.get(cid) == caldav_store.KIND_CALENDAR:
        cid = caldav_store.free_id(cid, known)
    if cid not in known:
        # Fatal, not a warning: without the metadata document the cards land under an id that
        # list_addressbooks never returns — they exist on the relay and appear nowhere at all.
        if not await caldav_store.put_calendar(
                db, current_user, cid,
                {"displayname": book or (file.filename or "Contacts").rsplit(".", 1)[0],
                 "kind": caldav_store.KIND_ADDRESSBOOK}):
            raise HTTPException(status_code=502, detail="Could not create the addressbook.")

    cards = caldav_store.split_vcards(text)
    if not cards:
        raise HTTPException(status_code=400, detail="No contacts found in that file.")
    if len(cards) > _IMPORT_MAX_ITEMS:
        raise HTTPException(status_code=413,
                            detail=f"That file holds {len(cards)} contacts; import up to "
                                   f"{_IMPORT_MAX_ITEMS} at a time.")

    import asyncio as _asyncio
    sem = _asyncio.Semaphore(8)

    async def _one(card):
        uid = caldav_store.uid_of_vcard(card) or secrets.token_hex(8)
        async with sem:
            return await caldav_store.put_item(db, current_user, cid, uid, card, "VCARD")

    results = await _asyncio.gather(*[_one(c) for c in cards], return_exceptions=True)
    added = sum(1 for r in results if r is True)
    skipped = len(results) - added
    _forget(current_user.username)
    for r in results:
        if isinstance(r, Exception):
            logger.warning("[carddav] import: a card failed: %s", r)
    return {"ok": True, "book": cid, "imported": added, "skipped": skipped}
