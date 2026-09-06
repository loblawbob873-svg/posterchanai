"""Private Media Center API. Every catalog and segment request checks the live ACL."""
import asyncio
import hashlib
import hmac
import json
import logging
import math
import secrets
import time
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field

from app.auth import get_admin_user, get_current_user
from app.services import media_center as media

PRIVATE = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}


class PrivateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def private(request: Request):
            response = await handler(request)
            response.headers.update(PRIVATE)
            response.headers["X-Accel-Buffering"] = "no"
            return response
        return private


router = APIRouter(prefix="/api/media-center", tags=["media-center"], route_class=PrivateRoute)
_scans = {}


class CreateLibrary(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    folder: str = Field(min_length=1, max_length=2048)
    encoder: Literal["auto", "cpu", "nvidia", "amd", "vaapi"] = "auto"


class Sharing(BaseModel):
    shared_with: list[str] = Field(default_factory=list, max_length=200)


class StopSession(BaseModel):
    ticket: str = Field(min_length=64, max_length=64)


@router.post("/sessions/stop")
async def stop_session(body: StopSession, user=Depends(get_current_user)):
    session = media._sessions.get(body.ticket)
    if session and session[0] == media.identity(user):
        media._sessions.pop(body.ticket, None)
    return {"ok": True}


class Limits(BaseModel):
    server_kbps: int = Field(default=20000, ge=650, le=1000000)
    viewer_kbps: int = Field(default=6000, ge=650, le=1000000)
    max_streams: int = Field(default=8, ge=1, le=100)
    max_transcodes: int = Field(default=2, ge=1, le=16)
    cache_mb: int = Field(default=2048, ge=32, le=1048576)


@router.get("/limits")
async def get_limits(user=Depends(get_admin_user)):
    return await media.limits()


@router.put("/limits")
async def set_limits(body: Limits, user=Depends(get_admin_user)):
    if body.viewer_kbps > body.server_kbps:
        raise HTTPException(400, "Per-user bandwidth cannot exceed the server limit")
    async with media.mutation_lock:
        await media.write("limits", body.model_dump())
    return body.model_dump()


async def library_for(library_id, pubkey, owner=False):
    library = await media.read("library:" + library_id)
    if not library or not media.can_read(library, pubkey):
        raise HTTPException(404, "Library not found")
    if owner and library["owner"] != pubkey:
        raise HTTPException(403, "Only the library owner can change it")
    return library


def public_library(library, pubkey):
    fields = ("id", "name", "owner", "count", "scanned_at", "skipped", "encoder")
    result = {key: library.get(key) for key in fields}
    if library["owner"] == pubkey:
        result.update(folder=library["folder"], shared_with=library["shared_with"])
    return result


async def save_scan(library):
    items, skipped = await asyncio.to_thread(media.scan, library["folder"], await media.catalog(library))
    pages = []
    # Small pages fit NIP-44's payload limit. Commit the manifest only after every
    # page is acknowledged, so readers retain the last complete scan on failure.
    batches, batch, byte_count = [], [], 2
    for item in items:
        size = len(json.dumps(item, separators=(",", ":")).encode()) + 1
        if batch and (byte_count + size > 48000 or len(batch) >= 100):
            batches.append(batch)
            batch, byte_count = [], 2
        batch.append(item)
        byte_count += size
    if batch:
        batches.append(batch)
    for page in batches:
        digest = hashlib.sha256(json.dumps(page, sort_keys=True).encode()).hexdigest()
        key = f"page:{library['id']}:{digest}"
        if key not in library.get("pages", []):
            await media.write(key, page)
        pages.append(key)
    async with media.mutation_lock:
        # Sharing can change during a long scan. Never overwrite the current ACL.
        current = await media.read("library:" + library["id"])
        updated = {**(current or library), "pages": pages, "count": len(items), "skipped": skipped, "scanned_at": int(time.time())}
        await media.write("library:" + library["id"], updated)
    return updated


async def run_scan(library):
    try:
        updated = await save_scan(library)
        _scans[library["id"]] = {"state": "complete", "count": updated["count"], "skipped": updated["skipped"]}
    except Exception:
        logging.getLogger(__name__).exception("Media Center scan failed")
        _scans[library["id"]] = {"state": "failed", "error": "Scan failed; check the folder, FFmpeg, and local relay"}


def queue_scan(library, background):
    if any(job["state"] == "running" for job in _scans.values()):
        raise HTTPException(409, "A media scan is already running; wait for it to finish")
    _scans[library["id"]] = {"state": "running"}
    background.add_task(run_scan, library)


@router.get("")
async def list_libraries(user=Depends(get_current_user)):
    pubkey = media.identity(user)
    config = await media.limits()
    return {"libraries": [public_library(lib, pubkey) for lib in await media.libraries() if media.can_read(lib, pubkey)],
            "can_create": bool(user.is_admin and pubkey), "profiles": media.allowed_profiles(config)}


@router.post("", status_code=201)
async def create_library(body: CreateLibrary, background: BackgroundTasks, user=Depends(get_admin_user)):
    pubkey = media.identity(user)
    if not pubkey:
        raise HTTPException(400, "Sign in with Nostr to own a library")
    try:
        folder = str(media.safe_root(body.folder))
        if not body.name.strip():
            raise ValueError("Library name cannot be blank")
        async with media.mutation_lock:
            if any(job["state"] == "running" for job in _scans.values()):
                raise HTTPException(409, "A media scan is already running; wait for it to finish")
            index = await media.read("index") or {"ids": []}
            if len(index["ids"]) >= 100:
                raise HTTPException(400, "Maximum of 100 libraries reached")
            library = {"id": secrets.token_hex(16), "name": body.name.strip(), "folder": folder,
                       "owner": pubkey, "shared_with": [], "encoder": body.encoder, "pages": []}
            await media.write("library:" + library["id"], library)
            await media.write("index", {"ids": index["ids"] + [library["id"]]})
            queue_scan(library, background)
        return public_library(library, pubkey)
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error


@router.get("/{library_id}/items")
async def items(library_id: str, user=Depends(get_current_user)):
    library = await library_for(library_id, media.identity(user))
    return {"items": [{k: v for k, v in item.items() if k not in ("path", "mtime_ns")}
                      for item in await media.catalog(library)]}


@router.get("/{library_id}/art/{item_id}")
async def artwork(library_id: str, item_id: str, user=Depends(get_current_user)):
    library = await library_for(library_id, media.identity(user))
    item = next((item for item in await media.catalog(library) if item["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Media not found")
    try:
        async with media.art_slots:
            path = await asyncio.to_thread(media.cover_path, library, item)
            if not path:
                raise HTTPException(404, "No cover artwork")
            stat = path.stat()
            data = await asyncio.to_thread(media.cover_bytes, path, stat.st_mtime_ns, stat.st_size)
        return Response(data, media_type="image/jpeg", headers=PRIVATE)
    except (OSError, ValueError) as error:
        raise HTTPException(404, "Cover artwork unavailable") from error


@router.post("/{library_id}/scan")
async def rescan(library_id: str, background: BackgroundTasks, user=Depends(get_current_user)):
    async with media.mutation_lock:
        library = await library_for(library_id, media.identity(user), owner=True)
        queue_scan(library, background)
        return {"state": "running"}


@router.get("/{library_id}/scan")
async def scan_status(library_id: str, user=Depends(get_current_user)):
    await library_for(library_id, media.identity(user), owner=True)
    return _scans.get(library_id, {"state": "idle"})


@router.put("/{library_id}/sharing")
async def share(library_id: str, body: Sharing, user=Depends(get_current_user)):
    async with media.mutation_lock:
        library = await library_for(library_id, media.identity(user), owner=True)
        try:
            library["shared_with"] = sorted({media.normalize_pubkey(key.strip()) for key in body.shared_with})
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        await media.write("library:" + library_id, library)
        return public_library(library, media.identity(user))


def sign_ticket(library, item_id, pubkey, expires):
    payload = f"media-center:{library['id']}:{item_id}:{pubkey}:{expires}"
    return hmac.new(bytes.fromhex(library["playback_secret"]), payload.encode(), hashlib.sha256).hexdigest()


@router.post("/{library_id}/play/{item_id}")
async def playback(library_id: str, item_id: str, user=Depends(get_current_user)):
    pubkey = media.identity(user)
    async with media.mutation_lock:
        library = await library_for(library_id, pubkey)
        if not any(item["id"] == item_id for item in await media.catalog(library)):
            raise HTTPException(404, "Media not found")
        if not library.get("playback_secret"):
            library["playback_secret"] = secrets.token_hex(32)
            await media.write("library:" + library_id, library)
    expires = int(time.time()) + 12 * 3600
    query = urlencode({"viewer": pubkey, "expires": expires, "ticket": sign_ticket(library, item_id, pubkey, expires)})
    try:
        media.touch_session(sign_ticket(library, item_id, pubkey, expires), pubkey, await media.limits())
    except RuntimeError as error:
        raise HTTPException(429, str(error)) from error
    return {"url": f"/api/media-center/{library_id}/hls/{item_id}/master.m3u8?{query}"}


@router.get("/{library_id}/hls/{item_id}/{asset}")
async def hls(library_id: str, item_id: str, asset: str, viewer: str = Query(max_length=64),
              expires: int = Query(), ticket: str = Query(max_length=64)):
    library = await library_for(library_id, viewer)
    if (expires < time.time() or not library.get("playback_secret") or
            not hmac.compare_digest(ticket, sign_ticket(library, item_id, viewer, expires))):
        raise HTTPException(403, "Playback session expired; reopen the media")
    query = urlencode({"viewer": viewer, "expires": expires, "ticket": ticket})
    config = await media.limits()
    profiles = media.allowed_profiles(config)
    try:
        media.touch_session(ticket, viewer, config)
    except RuntimeError as error:
        raise HTTPException(429, str(error)) from error
    if asset == "master.m3u8":
        lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for profile, (_, _, video, audio) in media.PROFILES.items():
            if profile not in profiles:
                continue
            lines += [f"#EXT-X-STREAM-INF:BANDWIDTH={int((video + audio) * 1200)}", f"{profile}.m3u8?{query}"]
        return Response("\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl", headers=PRIVATE)
    item = next((item for item in await media.catalog(library) if item["id"] == item_id), None)
    if not item:
        raise HTTPException(404, "Media not found")
    count = math.ceil(item["duration"] / media.SEGMENT)
    if asset.endswith(".m3u8") and asset[:-5] in profiles:
        profile = asset[:-5]
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{media.SEGMENT}",
                 "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
        for number in range(count):
            if number:
                lines.append("#EXT-X-DISCONTINUITY")
            duration = min(media.SEGMENT, item["duration"] - number * media.SEGMENT)
            lines += [f"#EXTINF:{duration:.6f},", f"{profile}-{number}.ts?{query}"]
        return Response("\n".join(lines + ["#EXT-X-ENDLIST", ""]), media_type="application/vnd.apple.mpegurl", headers=PRIVATE)
    try:
        profile, raw_number = asset.removesuffix(".ts").split("-")
        number = int(raw_number)
        if not asset.endswith(".ts") or profile not in profiles or not 0 <= number < count:
            raise ValueError()
    except ValueError:
        raise HTTPException(404, "Segment not found")
    try:
        data = await media.segment(library, item, profile, number, config)
        # Revocation also takes effect while an uncached segment is encoding.
        await library_for(library_id, viewer)
        return StreamingResponse(media.paced_bytes(data, viewer, config), media_type="video/mp2t", headers=PRIVATE)
    except asyncio.TimeoutError as error:
        raise HTTPException(503, "Transcoders are busy; retry shortly", headers={"Retry-After": "5"}) from error
    except (ValueError, OSError) as error:
        raise HTTPException(409, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(503, str(error)) from error
