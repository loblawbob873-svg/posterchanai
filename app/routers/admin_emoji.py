"""Admin → Custom Emoji: manage the instance's Pleroma/Akkoma-style emoji packs.

Thin router over `emoji_service` (which owns the on-disk pack format, including pack.json upkeep).
Kept out of `admin.py` because that module is one long settings/storage router and this is a
self-contained feature with file uploads of its own.

The public side — the picker's `/client/emojis` and the image URLs that end up inside published
NIP-30 tags — lives in `client.py` and needs no auth; everything here is admin-only.
"""
import logging
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import get_admin_user
from app.models import User
from app.services import emoji_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/emoji", tags=["admin"])


class EmojiRef(BaseModel):
    pack: str
    shortcode: str


class EmojiRename(EmojiRef):
    new_shortcode: str


class PackRef(BaseModel):
    name: str


@router.get("")
async def list_emoji(q: str = "", pack: str = "", offset: int = 0, limit: int = 300,
                     admin: User = Depends(get_admin_user)):
    """Packs + a filtered page of emoji. Paged because a real pack is thousands of entries — the UI
    searches and scrolls rather than downloading the whole index into the admin page."""
    entries = emoji_service.index()
    ql = (q or "").strip().lower().strip(":")
    if pack:
        entries = [e for e in entries if e["pack"] == pack]
    if ql:
        entries = [e for e in entries if ql in e["shortcode"].lower()]
    total = len(entries)
    limit = max(1, min(int(limit or 300), 1000))
    page = entries[max(0, int(offset or 0)):max(0, int(offset or 0)) + limit]
    st = emoji_service.stats()
    return JSONResponse({
        "dir": st["dir"], "exists": st["exists"], "count": st["count"], "bytes": st["bytes"],
        "packs": st["packs"], "total": total,
        "emojis": [{"s": e["shortcode"], "p": e["pack"],
                    "u": f"/client/emoji/{e['pack']}/{e['shortcode']}{e['ext']}",
                    "t": f"/client/emoji/{e['pack']}/{e['shortcode']}{e['ext']}?t=1"} for e in page],
    })


@router.post("/upload")
async def upload_emoji(pack: str = Form(emoji_service.ROOT_PACK),
                       shortcode: str = Form(""),
                       overwrite: bool = Form(False),
                       files: List[UploadFile] = File(...),
                       admin: User = Depends(get_admin_user)):
    """Upload one or many images into a pack. With several files the shortcode comes from each
    filename (bulk drag-and-drop of a downloaded pack); an explicit `shortcode` only applies when
    exactly one file is sent. Per-file errors are REPORTED, not fatal — one bad file in a drop of
    two hundred must not lose the other 199."""
    added, errors = [], []
    single = len(files) == 1
    for f in files:
        try:
            data = await f.read()
            res = emoji_service.add_emoji(pack, (shortcode if single else "") or "",
                                          f.filename or "", data, overwrite=bool(overwrite))
            added.append(res["shortcode"])
        except Exception as e:
            errors.append({"file": f.filename or "?", "error": str(e)})
        finally:
            await f.close()
    return JSONResponse({"ok": bool(added), "added": added, "errors": errors})


@router.post("/rename")
async def rename_emoji(body: EmojiRename, admin: User = Depends(get_admin_user)):
    try:
        return JSONResponse({"ok": True, **emoji_service.rename_emoji(body.pack, body.shortcode,
                                                                      body.new_shortcode)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/delete")
async def delete_emoji(body: EmojiRef, admin: User = Depends(get_admin_user)):
    try:
        emoji_service.delete_emoji(body.pack, body.shortcode)
        return JSONResponse({"ok": True})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/pack")
async def create_pack(body: PackRef, admin: User = Depends(get_admin_user)):
    try:
        return JSONResponse({"ok": True, "name": emoji_service.create_pack(body.name)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/pack/delete")
async def delete_pack(body: PackRef, admin: User = Depends(get_admin_user)):
    try:
        return JSONResponse({"ok": True, "removed": emoji_service.delete_pack(body.name)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
