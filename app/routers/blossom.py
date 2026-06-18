"""Built-in Blossom media server endpoints (BUD-01/02/06).

Mounted at `/blossom`, so a client points at `https://<host>/blossom` and the spec's
`GET <server>/<sha256>`, `PUT <server>/upload`, etc. resolve here. Front with TLS.

CPU-bound work (Schnorr verify, full-body sha256) is pushed off the event loop with
`asyncio.to_thread` so concurrent uploads from many users don't block the request loop.
The app has no global CORS middleware, so Blossom's required CORS headers are set here.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import BlossomBlob
from app.services import blossom_service
from app.services.nostr import nostr_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/blossom", tags=["blossom"])

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Content-Type, X-Content-Length, X-SHA-256, *",
    "Access-Control-Expose-Headers": "*",
}


def _err(status: int, reason: str) -> JSONResponse:
    # BUD-01: surface a human reason in X-Reason (clients display it) + a JSON body.
    return JSONResponse({"message": reason}, status_code=status,
                        headers={**_CORS, "X-Reason": reason})


def _strip_ext(token: str) -> str:
    return token.split(".", 1)[0].strip().lower()


def _base_url(request: Request, db: Session) -> str:
    cfg = blossom_service._cfg(db)
    if cfg["public_url"]:
        return cfg["public_url"]
    # Derive from the (proxied) request. Prefer forwarded headers set by nginx.
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/blossom"


@router.options("/{rest:path}")
async def blossom_preflight(rest: str = ""):
    return Response(status_code=204, headers=_CORS)


@router.put("/upload")
async def upload(request: Request, db: Session = Depends(get_db)):
    if not blossom_service.is_enabled(db):
        return _err(404, "Blossom server disabled")
    cfg = blossom_service._cfg(db)

    data = await request.body()
    if not data:
        return _err(400, "empty body")
    max_bytes = cfg["max_upload_mb"] * 1024 * 1024
    if len(data) > max_bytes:
        return _err(413, f"blob exceeds {cfg['max_upload_mb']} MB limit")

    sha256 = await asyncio.to_thread(blossom_service.compute_sha256, data)
    try:
        pubkey = await asyncio.to_thread(
            blossom_service.verify_auth, request.headers.get("authorization", ""), "upload", sha256)
    except ValueError as e:
        return _err(401, str(e))

    if not blossom_service.is_pubkey_allowed(db, pubkey):
        return _err(403, "not authorized to upload (needs the can_blossom privilege)")

    mime = request.headers.get("content-type", "") or "application/octet-stream"
    try:
        await blossom_service.save_blob(db, pubkey, data, mime)
    except Exception as e:
        logger.error("[blossom] upload failed: %s", e, exc_info=True)
        return _err(500, "storage error")

    blob = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
    return JSONResponse(blossom_service.descriptor(blob, _base_url(request, db)), headers=_CORS)


@router.head("/upload")
async def upload_requirements(request: Request, db: Session = Depends(get_db)):
    """BUD-06: pre-flight an upload without sending the body. Validates auth + size."""
    if not blossom_service.is_enabled(db):
        return Response(status_code=404, headers={**_CORS, "X-Reason": "Blossom server disabled"})
    cfg = blossom_service._cfg(db)
    try:
        size = int(request.headers.get("x-content-length", "0"))
    except ValueError:
        size = 0
    if size and size > cfg["max_upload_mb"] * 1024 * 1024:
        return Response(status_code=413, headers={**_CORS, "X-Reason": f"max {cfg['max_upload_mb']} MB"})
    try:
        pubkey = await asyncio.to_thread(
            blossom_service.verify_auth, request.headers.get("authorization", ""), "upload")
    except ValueError as e:
        return Response(status_code=401, headers={**_CORS, "X-Reason": str(e)})
    if not blossom_service.is_pubkey_allowed(db, pubkey):
        return Response(status_code=403, headers={**_CORS, "X-Reason": "not authorized to upload"})
    return Response(status_code=200, headers=_CORS)


@router.get("/list/{pubkey}")
async def list_blobs(pubkey: str, request: Request, db: Session = Depends(get_db)):
    if not blossom_service.is_enabled(db):
        return _err(404, "Blossom server disabled")
    pk_hex = nostr_service.to_pubkey_hex(pubkey)
    if not pk_hex:
        return _err(400, "invalid pubkey")
    base = _base_url(request, db)
    blobs = blossom_service.list_for_pubkey(db, pk_hex)
    return JSONResponse([blossom_service.descriptor(b, base) for b in blobs], headers=_CORS)


@router.api_route("/{sha256}", methods=["GET", "HEAD"])
async def get_blob(sha256: str, request: Request, db: Session = Depends(get_db)):
    if not blossom_service.is_enabled(db):
        return _err(404, "Blossom server disabled")
    sha = _strip_ext(sha256)
    blob = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha).first()
    if not blob:
        return _err(404, "blob not found")

    headers = {
        **_CORS,
        "Content-Length": str(blob.size),
        "Cache-Control": "public, max-age=31536000, immutable",
        "Accept-Ranges": "none",
    }
    if request.method == "HEAD":
        return Response(status_code=200, media_type=(blob.mime or "application/octet-stream"),
                        headers=headers)

    result = await blossom_service.read_blob(db, blob)
    if result is None:
        return _err(404, "blob bytes unavailable")
    stream, mime, _size = result
    return StreamingResponse(stream, media_type=mime, headers=headers)


@router.delete("/{sha256}")
async def delete_blob(sha256: str, request: Request, db: Session = Depends(get_db)):
    if not blossom_service.is_enabled(db):
        return _err(404, "Blossom server disabled")
    sha = _strip_ext(sha256)
    blob = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha).first()
    if not blob:
        return _err(404, "blob not found")
    try:
        pubkey = await asyncio.to_thread(
            blossom_service.verify_auth, request.headers.get("authorization", ""), "delete", sha)
    except ValueError as e:
        return _err(401, str(e))
    # Only the owner (or any admin holding the key) may delete.
    if pubkey != blob.pubkey and not blossom_service.is_pubkey_allowed(db, pubkey):
        return _err(403, "not authorized to delete this blob")
    if pubkey != blob.pubkey:
        # An allowed key that isn't the owner: still require admin to delete others' blobs.
        from app.models import User
        u = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pubkey)).first()
        if not (u and getattr(u, "is_admin", False)):
            return _err(403, "only the owner or an admin may delete this blob")

    await blossom_service.delete_blob_bytes(db, blob)
    db.delete(blob)
    db.commit()
    return JSONResponse({"message": "deleted", "sha256": sha}, headers=_CORS)
