"""Built-in Blossom media server endpoints (BUD-01/02/06).

Mounted at `/blossom`, so a client points at `https://<host>/blossom` and the spec's
`GET <server>/<sha256>`, `PUT <server>/upload`, etc. resolve here. Front with TLS.

CPU-bound work (Schnorr verify, full-body sha256) is pushed off the event loop with
`asyncio.to_thread` so concurrent uploads from many users don't block the request loop.
The app has no global CORS middleware, so Blossom's required CORS headers are set here.
"""

import asyncio
import logging
import re
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, Request, Response, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import BlossomBlob
from app.services import blossom_service, tor_service
from app.services.nostr import nostr_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/blossom", tags=["blossom"])

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Content-Type, X-Content-Length, X-SHA-256, X-Filename, X-No-Mirror, X-Keep, *",
    "Access-Control-Expose-Headers": "*",
}


def _err(status: int, reason: str) -> JSONResponse:
    # BUD-01: surface a human reason in X-Reason (clients display it) + a JSON body.
    return JSONResponse({"message": reason}, status_code=status,
                        headers={**_CORS, "X-Reason": reason})


# Small in-RAM thumbnail cache for the Files grid (`?thumb=1`). Thumbs are tiny (~10-20 KB), so a
# few hundred entries is a few MB — bounded to avoid unbounded growth. Saves re-encoding + serving
# full-resolution images just to render a grid cell.
import os
from collections import OrderedDict
import threading
_thumb_cache: "OrderedDict[str, bytes]" = OrderedDict()
_thumb_lock = threading.Lock()
_THUMB_MAX = 400
# DISK tier: persists thumbnails across restarts so an expensive video ffmpeg / image decode runs ONCE
# ever — not on every restart or re-list (the cause of one user pegging a core). Tiny JPEGs keyed by
# sha, plus a `.none` sentinel for undecodable blobs.
_THUMB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "blossom_thumbs")
# Largest blob we will buffer whole in order to MAKE a thumbnail. Generation needs the bytes in RAM
# (both ffmpeg and PIL do), in the single uvicorn worker, on a route that needs no authentication —
# so the ceiling is what stops one URL from allocating a node's memory. It matters most for untyped
# blobs, which are the encrypted drive, the music library and the folder-sync manifests: the class
# where the multi-gigabyte objects actually are. Past this a blob is served as itself.
_THUMB_MAX_SRC = 256 * 1024 * 1024
# Cap concurrent thumbnail GENERATION (ffmpeg/Pillow are CPU-bound). Without it a folder of videos
# spawned ~one ffmpeg per pool thread and pegged every core; 3 leaves headroom for serving requests.
_thumb_sem = asyncio.Semaphore(3)


def _thumb_disk_path(sha: str, ok: bool = True) -> str:
    return os.path.join(_THUMB_DIR, sha + (".jpg" if ok else ".none"))


def _thumb_put_ram(sha: str, data: bytes) -> None:
    with _thumb_lock:
        _thumb_cache[sha] = data
        _thumb_cache.move_to_end(sha)
        while len(_thumb_cache) > _THUMB_MAX:
            _thumb_cache.popitem(last=False)


def _thumb_get(sha: str):
    with _thumb_lock:
        t = _thumb_cache.get(sha)
        if t is not None:
            _thumb_cache.move_to_end(sha)
            return t
    # Disk tier — survives restarts so we never regenerate. `.none` = cached "undecodable" sentinel.
    try:
        if os.path.exists(_thumb_disk_path(sha, ok=False)):
            _thumb_put_ram(sha, b"")
            return b""
        p = _thumb_disk_path(sha, ok=True)
        if os.path.exists(p):
            with open(p, "rb") as f:
                d = f.read()
            _thumb_put_ram(sha, d)
            return d
    except Exception:
        pass
    return None


def _thumb_put(sha: str, data: bytes) -> None:
    _thumb_put_ram(sha, data)
    try:
        os.makedirs(_THUMB_DIR, exist_ok=True)
        if data:
            tmp = _thumb_disk_path(sha) + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, _thumb_disk_path(sha, ok=True))
        else:
            open(_thumb_disk_path(sha, ok=False), "wb").close()   # undecodable sentinel
    except Exception:
        pass


def _video_thumb_bytes(data: bytes, size: int = 320) -> "bytes | None":
    """Extract a single frame from video bytes via ffmpeg → downscaled JPEG. Returns None if ffmpeg
    is missing or the video can't be decoded (the Files grid then falls back to a 🎬 icon). Tries a
    1s seek first (avoids a black opening frame), then t=0 for very short clips."""
    import subprocess, tempfile, os
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5, check=True)
    except Exception:
        return None
    tin = tout = None
    try:
        with tempfile.NamedTemporaryFile(prefix="pcai_blossom_vid_", suffix=".vid", delete=False) as f:
            f.write(data)
            tin = f.name
        tout = tin + ".jpg"
        vf = f"scale={size}:-2"
        for ss in ("1", "0"):
            try:
                subprocess.run(["ffmpeg", "-y", "-ss", ss, "-i", tin, "-frames:v", "1",
                                "-vf", vf, "-f", "image2", tout],
                               capture_output=True, timeout=20)
            except Exception:
                continue
            if os.path.exists(tout) and os.path.getsize(tout) > 0:
                with open(tout, "rb") as f:
                    return f.read()
        return None
    except Exception:
        return None
    finally:
        for p in (tin, tout):
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


def _strip_ext(token: str) -> str:
    return token.split(".", 1)[0].strip().lower()


def _safe_filename(name: str) -> str:
    """Sanitise a filename that will end up in a Content-Disposition header (see blossom_service)."""
    return blossom_service.safe_filename(name)


async def _read_head(db: Session, blob, n: int = 16) -> bytes:
    """The first `n` bytes of a blob, for the magic-number sniffs — and only those bytes.

    The obvious alternative, `read_full`, is a trap on exactly the blobs that need sniffing: an
    untyped blob is what every encrypted-drive upload, music track and folder-sync manifest is, so
    the class is the LARGEST one stored. Buffering a 2 GB `.enc` into the single uvicorn worker to
    look at sixteen bytes is enough to OOM a 12 GB node from one unauthenticated blob URL — and a
    Files grid of them does it once per tile.
    """
    head = await blossom_service.read_range(db, blob, 0, n - 1)
    if head is None:
        return b""
    buf = b""
    try:
        async for chunk in head:
            buf += chunk
            if len(buf) >= n:
                break
    except Exception:
        return buf
    finally:
        # On the proxy backend this iterator owns an open upstream response; abandoning it
        # mid-stream would hold the connection until GC.
        aclose = getattr(head, "aclose", None)
        if aclose:
            try:
                await aclose()
            except Exception:
                pass
    return buf


def _disposition(disp: str, name: str) -> str:
    """A Content-Disposition value that survives a non-ASCII filename.

    Header values are encoded latin-1 by the ASGI layer, so putting `写真.png` straight in the header
    raises UnicodeEncodeError and turns a download into a 500. RFC 6266/5987 is the way out: an
    ASCII-only `filename=` for old clients plus a percent-encoded `filename*=` that every current
    browser prefers."""
    ascii_name = name.encode("ascii", "ignore").decode("ascii").strip() or "download"
    out = f'{disp}; filename="{ascii_name}"'
    if ascii_name != name:
        out += "; filename*=UTF-8''" + quote(name, safe="")
    return out


def _upload_filename(request: Request) -> str:
    """The uploader's original filename, if they sent one.

    BUD-01 has no filename field (a blob is its hash), so this is best-effort: `X-Filename`
    (optionally percent-encoded, which is how a non-ASCII name survives a header) or a standard
    `Content-Disposition: …; filename="…"`. Missing is normal and fine."""
    raw = (request.headers.get("x-filename", "") or "").strip()
    if not raw:
        cd = request.headers.get("content-disposition", "") or ""
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.I)
        raw = m.group(1).strip() if m else ""
    if not raw:
        return ""
    if "%" in raw:
        try:
            raw = unquote(raw)
        except Exception:
            pass
    return _safe_filename(raw)


def _base_url(request: Request, db: Session) -> str:
    # An upload over our .onion must come back as an onion URL: this string is what the client puts in
    # the note / hands to other users, so returning the clearnet media host would exit Tor for every
    # view AND stamp the instance's real domain into an onion user's posts. Mirrors client._blossom_url.
    onion = tor_service.request_onion_host(request)
    if onion:
        return f"http://{onion}/blossom"
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

    # Per-user quota — AFTER authorization, so someone who may not upload at all gets a plain 403
    # rather than a confusing quota message (and we don't run the aggregate for them). Skipped unless
    # an admin set one, so the default costs nothing. It exists because blobs are now kept forever
    # (blossom_blob_ttl_days=0): with no age sweep and no cap, one uploader can fill the disk for
    # every other user on the node.
    over, used, limit = blossom_service.quota_exceeded(db, pubkey, len(data))
    if over:
        return _err(413, f"storage quota reached ({used // (1024**3)} of {limit // (1024**3)} GB used)")

    mime = request.headers.get("content-type", "") or "application/octet-stream"
    # X-No-Mirror: client opt-out of DR mirroring (encrypted music — don't push it to public backups).
    no_mirror = request.headers.get("x-no-mirror", "") in ("1", "true", "yes")
    # X-Keep: this blob is client-side encrypted DRIVE content (Notes attachment, music track, the
    # files index) — the only copy of bytes nobody but its owner can read. Exempt it from the age
    # sweep permanently. Only the uploader can know this: to the server the bytes are opaque.
    keep = request.headers.get("x-keep", "") in ("1", "true", "yes")
    filename = _upload_filename(request)
    try:
        await blossom_service.save_blob(db, pubkey, data, mime, mirror=not no_mirror,
                                        filename=filename, keep=keep)
    except Exception as e:
        logger.error("[blossom] upload failed: %s", e, exc_info=True)
        return _err(500, "storage error")

    blob = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
    return JSONResponse(
        blossom_service.descriptor(blob, _base_url(request, db),
                                   name=blossom_service.name_for(db, sha256, pubkey)),
        headers=_CORS)


@router.api_route("/upload", methods=["HEAD", "GET"])
async def upload_requirements(request: Request, db: Session = Depends(get_db)):
    """BUD-06: pre-flight an upload without sending the body. Validates auth + size.

    GET IS ACCEPTED BECAUSE A CACHING PROXY REWRITES THE METHOD. BUD-06 says HEAD, and the client
    sends HEAD — but nginx turns a HEAD into a GET upstream when proxy caching is on, so behind
    media.poster.place this arrived as `GET /blossom/upload` and matched no route at all. That is a
    FastAPI 404, not this router's, so it carries none of the CORS headers every response here sets,
    and the browser reports the only thing it can see:

        Access to fetch at 'https://media.poster.place/upload' from origin 'app://posterchan'
        blocked by CORS policy: No 'Access-Control-Allow-Origin' header is present

    which is a CORS error about a route that does not exist, for a request nobody sent. It cost this
    session an hour of checking CORS configuration that was correct throughout. The probe itself is
    harmless — both callers catch and fall through — so the damage was entirely in the diagnosis.
    """
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
    names = blossom_service.names_for_pubkey(db, pk_hex)   # one query, not one per blob
    # no-store: this listing changes the moment a user uploads or deletes, and it carried NO cache
    # headers at all — which leaves a browser (or an upstream proxy) free to apply heuristic freshness
    # and serve a stale drive. That reads as "I deleted a file and Files didn't update".
    return JSONResponse([blossom_service.descriptor(b, base, name=names.get(b.sha256, ""))
                         for b in blobs],
                        headers={**_CORS, "Cache-Control": "no-store, max-age=0"})


@router.api_route("/thumb/{sha256}", methods=["GET", "HEAD"])
async def get_blob_thumb(sha256: str, request: Request, db: Session = Depends(get_db)):
    """A blob's preview JPEG, on its OWN PATH — the only reason this route exists.

    Thumbnails used to be `<blob-url>?thumb=1`, and caches key on the PATH: Cloudflare (and the origin
    nginx, which sets x-cache-status) both ignored the query, so `<sha>.mp4` and `<sha>.mp4?thumb=1`
    shared ONE cache entry. Whichever was fetched first won — pinned for a year by the
    `immutable, max-age=31536000` these responses carry. Measured through the public edge:

        GET /<sha>.mp4?thumb=1  ->  200 video/mp4  1,612,155 bytes   cf-cache-status: HIT

    i.e. the Files grid's <img> was handed the whole MP4, could not decode it, and fell back to the
    🎬 icon. FOREVER, for that blob, at that edge. Images never showed it because the collision hands
    them the full-size image, which renders perfectly well — which is exactly why this looked like
    "video thumbnails are broken and image ones are fine", and why it seemed to depend on the browser
    or on Tor: it depends only on which of the two URLs that edge happened to cache first.

    A distinct path cannot collide with the blob, so `immutable` is now honest here.
    """
    # One implementation, asked explicitly for a thumbnail — not by rewriting the request's query
    # string underneath itself, which works only until something reads query_params first.
    return await _serve_blob(sha256, request, db, force_thumb=True)


@router.api_route("/{sha256}", methods=["GET", "HEAD"])
async def get_blob(sha256: str, request: Request, db: Session = Depends(get_db)):
    return await _serve_blob(sha256, request, db)


# The implementation both routes share. `force_thumb` lives HERE and not in a route signature, because
# FastAPI turns a route's plain arguments into query parameters — it would have become a public
# ?force_thumb=… on every blob URL, and a second spelling of the thing whose spelling caused the bug.
async def _serve_blob(sha256: str, request: Request, db: Session, force_thumb: bool = False):
    if not blossom_service.is_enabled(db):
        return _err(404, "Blossom server disabled")
    sha = _strip_ext(sha256)
    # Metadata cache: a hot blob's row is served from RAM, so a GET/HEAD skips Postgres entirely
    # (the row is immutable — content-addressed). Only a cache miss touches the DB.
    blob = blossom_service.get_blob_meta(db, sha)
    if not blob:
        return _err(404, "blob not found")

    mime = blob.mime or "application/octet-stream"
    headers = {
        **_CORS,
        "Cache-Control": "public, max-age=31536000, immutable",
        "Accept-Ranges": "bytes",   # advertise range support so browsers will seek/play video
    }
    # IS THIS BLOB ON DEATH ROW? Folder sync asks with a HEAD before uploading, and skips the body
    # when the bytes are already here — which also skips save_blob, and save_blob is what CLEARS an
    # expiry when a blob becomes referenced again. Without this the sweep could record a manifest
    # entry pointing at bytes already scheduled for deletion, and that file would go missing from
    # every device a week later with nothing said. Advertised so the client can decline to skip.
    # `Access-Control-Expose-Headers: *` above is what lets it read this cross-origin.
    if getattr(blob, "expires_at", None):
        headers["X-Expires-At"] = str(int(blob.expires_at))
    # Name the download. `inline` keeps images/video previewing in the tab (an `attachment` would
    # force a download on every view), but a "Save as" — and the old EXTENSIONLESS /<sha> links
    # already living in notes — now lands as `report.pdf` (or `<sha>.pdf`) instead of a bare hash.
    # Precedence: an explicit ?filename= (the caller knows best) → the uploader's stored name →
    # sha + the extension implied by the MIME type. `?download=1` flips it to a forced download.
    _dl = bool(request.query_params.get("download"))
    _fname = _safe_filename(request.query_params.get("filename", ""))
    # The stored-name lookup is a DB round-trip, so it's reserved for an actual download — the hot
    # path here is thumbnails and inline media, which the metadata RAM cache deliberately serves
    # without touching Postgres.
    if not _fname and _dl:
        _fname = _safe_filename(blossom_service.name_for(db, sha))
    if not _fname:
        _ext = blossom_service.ext_for_mime(mime)
        _fname = sha + (f".{_ext}" if _ext else "")
    if _dl and not re.search(r"\.[A-Za-z0-9]{1,8}$", _fname):
        # Still nameless: the MIME is generic (octet-stream) and nobody told us a filename. The bytes
        # themselves are the last honest source — 16 of them, and only on an explicit download, so the
        # thumbnail/inline hot path never pays for it.
        _ext = blossom_service.sniff_ext(await _read_head(db, blob))
        if _ext:
            _fname += f".{_ext}"
    headers["Content-Disposition"] = _disposition("attachment" if _dl else "inline", _fname)
    if request.method == "HEAD":
        return Response(status_code=200, media_type=mime,
                        headers={**headers, "Content-Length": str(blob.size)})

    # ?thumb=1 → a downscaled JPEG for grid cells (saves serving the full-res image). Cached in RAM.
    # Images compress directly; videos get an ffmpeg-extracted frame (covers old + new uploads with no
    # batch step — the grid requests it on demand). An empty-bytes sentinel caches "no thumbnail" so a
    # video ffmpeg can't decode isn't re-run on every render.
    # An UNTYPED blob gets in too. `new File([bytes], 'photo.jpg')` has `type === ''` — the
    # constructor never looks at the name — so anything that rebuilt a file from raw bytes without
    # passing a type explicitly stored real media as `application/octet-stream`. Gated on the stored
    # MIME alone, those blobs could never have a thumbnail made: the branch was skipped, the full
    # file was served to an <img>, and the drive drew a paperclip for a photograph. The write paths
    # are fixed, but a blob is immutable and content-addressed, so the ones already on people's
    # drives keep the wrong type for ever and the read path has to cope.
    #
    # THE SNIFF READS SIXTEEN BYTES, NOT THE BLOB. Doing it after read_full would have been free only
    # if every candidate were small — and the untyped class is the opposite: every encrypted-drive
    # file, music track and sync manifest is octet-stream, so it is where the multi-gigabyte blobs
    # are. See _read_head.
    #
    # And a blob the table CANNOT name is not decided here at all. sniff_ext knows thirteen magic
    # numbers; a bmp/tiff/ico/svg falls through it, and treating that as "no thumbnail" would write a
    # sentinel to DISK — permanent, surviving any later fix — for a file PIL can decode perfectly
    # well. Falling out of the branch instead serves the original bytes, which is what these blobs
    # did before this feature existed: a real picture, just not a downscaled one.
    _generic = (not blob.mime) or mime.startswith("application/octet-stream")
    if force_thumb or request.query_params.get("thumb"):
        # THE CACHE ANSWERS FIRST, for a blob of ANY type. Deciding the kind before looking meant a
        # generic blob whose thumbnail was already made took the sniff-skipping path on every later
        # request — `_tmime` stayed octet-stream, the branch was not entered, and the THUMBNAIL url
        # answered with the whole original file. Under the `immutable, max-age=31536000` these
        # responses carry, that pins a multi-megabyte blob to the thumbnail URL at the edge for a
        # year: exactly the cache poisoning the separate /thumb/ path exists to prevent, and for a
        # video the <img> cannot decode it, so the tile reverts to an icon after its first view.
        t = _thumb_get(sha)
        if t is None:
            _tmime = (blossom_service.sniff_mime(await _read_head(db, blob)) or mime) if _generic else mime
            # Generating one means holding the whole blob in RAM (ffmpeg and PIL both want the bytes),
            # in the single uvicorn worker. That was survivable while only DECLARED media got here;
            # untyped blobs are the encrypted drive, the music library and the sync manifests, which
            # is where the multi-gigabyte objects live, and /thumb/<sha> needs no authentication. So
            # anything oversized falls through and is served as itself rather than buffered.
            if (_tmime.startswith("image/") or _tmime.startswith("video/")) \
                    and (blob.size or 0) <= _THUMB_MAX_SRC:
                async with _thumb_sem:           # bound concurrent generation so a list can't peg cores
                    t = _thumb_get(sha)          # re-check: a concurrent request may have just made it
                    if t is None:
                        data = await blossom_service.read_full(db, blob)
                        if data is None:
                            blossom_service.revalidate_meta(db, sha)   # self-heal ONLY if the row is truly gone (not a transient outage)
                            return _err(404, "blob bytes unavailable")
                        if _tmime.startswith("video/"):
                            t = await asyncio.to_thread(_video_thumb_bytes, data, 320)
                            _thumb_put(sha, t if t is not None else b"")   # sentinel: don't retry a failed decode
                        else:
                            try:
                                from app.services.media_service import compress_image
                                t = await asyncio.to_thread(compress_image, data, 320, 70)
                            except Exception:
                                t = data   # undecodable → fall back to the original bytes
                            _thumb_put(sha, t)
        # t is None only when there is no preview to make (not media, or too large to buffer) — fall
        # through and serve the blob itself, which is what these did before thumbnails existed.
        if t is not None:
            if not t:   # cached no-thumbnail sentinel (video undecodable / ffmpeg missing) — cache the
                # negative so the grid's <img> onerror→icon fallback doesn't re-request it every render
                return Response(status_code=404, headers={**_CORS, "Cache-Control": "public, max-age=86400"})
            return Response(t, media_type="image/jpeg",
                            headers={**headers, "Content-Length": str(len(t)),
                                     # this response is a JPEG preview, not the blob — don't hand the
                                     # browser the full file's name for it
                                     "Content-Disposition": _disposition("inline", f"{sha}.jpg")})

    # HTTP Range (video/audio seeking + MP4s with a trailing moov atom). Streams ONLY the requested
    # window — from the RAM cache (slice), the storage proxy (Range forwarded → nas 206), or a local
    # file seek — so a seek never re-downloads the whole blob (the old buffer-the-whole-thing path).
    rng = request.headers.get("range")
    if rng and rng.startswith("bytes=") and blob.size:
        total = blob.size
        try:
            s, _, e = rng[6:].partition("-")
            if s == "" and e:            # suffix range: "bytes=-N" ⇒ the LAST N bytes
                start = max(0, total - int(e))
                end = total - 1
            else:
                start = int(s) if s else 0
                end = int(e) if e else total - 1
        except ValueError:
            start, end = 0, total - 1
        end = min(end, total - 1)
        if start > end or start < 0:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{total}"})
        body = await blossom_service.read_range(db, blob, start, end)
        if body is None:
            blossom_service.revalidate_meta(db, sha)   # self-heal ONLY if the row is truly gone (not a transient outage)
            return _err(404, "blob bytes unavailable")
        return StreamingResponse(body, status_code=206, media_type=mime,
                                 headers={**headers, "Content-Range": f"bytes {start}-{end}/{total}",
                                          "Content-Length": str(end - start + 1)})

    result = await blossom_service.read_blob(db, blob)
    if result is None:
        blossom_service.revalidate_meta(db, sha)   # self-heal ONLY if the row is truly gone (not a transient outage)
        return _err(404, "blob bytes unavailable")
    stream, rmime, size = result
    return StreamingResponse(stream, media_type=rmime,
                             headers={**headers, "Content-Length": str(size)})


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
    # Who may delete: anyone who OWNS a reference (dedup means that is no longer just the first
    # uploader — listing by ownership without this would show a second owner a file they then got a
    # 403 deleting), or an admin, whose delete is a moderation purge for everyone.
    owns = blossom_service.is_owner(db, sha, pubkey)
    is_admin = False
    if not owns:
        if not blossom_service.is_pubkey_allowed(db, pubkey):
            return _err(403, "not authorized to delete this blob")
        from app.models import User
        u = db.query(User).filter(User.nostr_npub == nostr_service.npub_of(pubkey)).first()
        is_admin = bool(u and getattr(u, "is_admin", False))
        if not is_admin:
            return _err(403, "only an owner or an admin may delete this blob")

    if owns:
        # Drop just THIS user's reference. Blossom dedups, so the same bytes can be referenced by
        # several people, and deleting the row outright removed the file from everyone else's drive.
        # The bytes go only once the last owner lets go.
        remaining = blossom_service.release_owner(db, sha, pubkey)
        if remaining > 0:
            db.commit()
            blossom_service.drop_meta(sha)
            return JSONResponse({"message": "deleted", "sha256": sha, "shared": True}, headers=_CORS)

    await blossom_service.delete_blob_bytes(db, blob)
    db.delete(blob)
    db.commit()
    blossom_service.drop_meta(sha)   # AFTER commit: the row is gone, so a re-query can't re-cache it
    return JSONResponse({"message": "deleted", "sha256": sha}, headers=_CORS)
