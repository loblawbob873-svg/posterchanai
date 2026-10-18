"""Feed thumbnails: a remote picture resized to the size the client actually draws it at.

Measured 2026-10-16 on poster.place's Nostrverse at phone width (105 cards, scrolled): the feed's images held
619 MB once decoded, against 72 MB needed for the size they are shown at -- 111 avatars of 22-44 px were 319 MB of
it (a 1500x1500 original behind every one). On a phone that is far past what the WebView keeps rasterised, and a
fast scroll showed bare background where cards should be ("choppy scroll, you see black on the timeline"). Native
Nostr clients draw from resized copies; this is that, served by the instance the client already talks to.

Rules:
  * the fetch is the image proxy's: the SSRF guard on every redirect hop, a total size cap, an image type only;
  * widths are a fixed ladder (no per-request sizes to fill the cache with);
  * anything that should not be resized -- an animation, a picture already no larger than asked, something Pillow
    cannot read, a fetch that failed -- answers None and the route REDIRECTS to the original, so the worst case is
    exactly what the client did before;
  * results are cached on disk under a size cap (oldest first out), so a scroll back costs a file read.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os

logger = logging.getLogger(__name__)

WIDTHS = (48, 96, 160, 320, 480, 720, 1080)
MAX_SOURCE = 25 * 1024 * 1024           # bytes fetched for one picture
MAX_PIXELS = 60_000_000                  # decompression-bomb guard
CACHE_CAP = int(os.environ.get("POSTERCHANAI_THUMB_CACHE_MB", "512")) * 1024 * 1024
_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "thumbs")
_sem = asyncio.Semaphore(3)              # resizes at once: a cheap VPS must stay responsive
_writes = 0
MAX_PENDING = 400                        # thumbnails queued for the background; beyond it the original is served
_pending: dict = {}


def snap(w) -> int:
    """The ladder width at or above `w` (the largest when beyond it)."""
    try:
        w = int(w)
    except (TypeError, ValueError):
        w = WIDTHS[3]
    for x in WIDTHS:
        if x >= w:
            return x
    return WIDTHS[-1]


def _path(url: str, w: int) -> str:
    k = hashlib.sha256(("%d:%s" % (w, url)).encode("utf-8", "surrogatepass")).hexdigest()[:40]
    return os.path.join(_ROOT, k[:2], k + ".webp")


def resize(data: bytes, w: int):
    """WebP bytes of `data` scaled to width `w`, or None when it should be served as it is."""
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        im = Image.open(io.BytesIO(data))
        if getattr(im, "is_animated", False) and getattr(im, "n_frames", 1) > 1:
            return None                                   # an animation stays an animation
        if im.width <= w:
            return None                                   # already small: the original is the thumbnail
        im.draft("RGB", (w, max(1, round(im.height * w / im.width))))   # JPEG: decode at a fraction of full size
        im = ImageOps.exif_transpose(im)
        if im.width <= w:
            return None
        h = max(1, round(im.height * w / im.width))
        alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
        im = im.convert("RGBA" if alpha else "RGB")
        im = im.resize((w, h), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "WEBP", quality=80, method=4)
        return out.getvalue()
    except Exception as e:                                # noqa: BLE001 -- not readable as a still picture
        logger.debug("[thumb] not resized: %s", e)
        return None


async def _fetch(url: str):
    import httpx
    from app.services.search_service import is_safe_url
    from app.utils import redirect_guard
    ok, _err = is_safe_url(url)
    if not ok:
        return None
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0), follow_redirects=True,
                                 transport=redirect_guard.PinnedTransport(),     # no DNS rebinding past the guard
                                 event_hooks=redirect_guard.HOOKS) as client:
        async with client.stream("GET", url, headers={"Accept": "image/*,*/*;q=0.5",
                                                       "User-Agent": "Mozilla/5.0 PosterChan thumbnailer"}) as r:
            if r.status_code >= 400:
                return None
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            if ctype and not ctype.startswith("image/") and ctype != "application/octet-stream":
                return None
            if int(r.headers.get("content-length") or 0) > MAX_SOURCE:
                return None
            buf = bytearray()
            async for chunk in r.aiter_bytes(65536):
                buf += chunk
                if len(buf) > MAX_SOURCE:
                    return None
            return bytes(buf)


def _prune() -> None:
    """Keep the cache under its cap, oldest files first."""
    files = []
    total = 0
    for d, _sub, names in os.walk(_ROOT):
        for n in names:
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            # An empty "use the original" marker still costs an inode and a block: counted as one, or a stream of
            # junk URLs (each a marker) grew the directory without limit while the byte total stayed at zero.
            size = max(st.st_size, 4096)
            files.append((st.st_mtime, size, p))
            total += size
    if total <= CACHE_CAP:
        return
    files.sort()
    for _m, size, p in files:
        if total <= CACHE_CAP * 0.8:
            break
        try:
            os.remove(p)
            total -= size
        except OSError:
            pass


def peek(url: str, w: int):
    """What the cache already knows, without a fetch: WebP bytes, b"" (the original is right), or None (not made)."""
    try:
        with open(_path(url, snap(w)), "rb") as f:
            return f.read()
    except OSError:
        return None


def schedule(url: str, w: int) -> None:
    """Make the thumbnail in the background (once per url+width). A client is NEVER made to wait for it.

    2026-10-16: the route resized inline -- a picture not seen before cost ~2.5 s (download the original, resize), three
    at a time, and the service worker's own re-fetch of every image paid it twice. A first scroll through new posts
    showed empty image boxes for 15 s+ ("vyram said it's worse now"). Now the first view gets the original at once (what
    the client did before thumbnails), and every view after it gets the small copy."""
    w = snap(w)
    key = (w, url)
    if key in _pending or len(_pending) >= MAX_PENDING:
        return
    try:
        task = asyncio.get_running_loop().create_task(thumb(url, w))
    except RuntimeError:
        return
    _pending[key] = task
    task.add_done_callback(lambda _t, k=key: _pending.pop(k, None))


async def thumb(url: str, w: int):
    """WebP bytes for `url` at ladder width `w`, or None (serve the original)."""
    global _writes
    w = snap(w)
    p = _path(url, w)
    try:
        with open(p, "rb") as f:
            data = f.read()
        if data:
            return data
        return None                                       # an empty marker: "the original is right"
    except OSError:
        pass
    async with _sem:
        try:
            src = await _fetch(url)
        except Exception as e:                            # noqa: BLE001 -- a host that will not answer
            logger.debug("[thumb] fetch failed: %s", e)
            src = None
        # A source that could not be had is remembered as "the original is right" too: the original is what the
        # client then loads anyway, and without the marker every view would queue the same failing fetch again.
        out = await asyncio.to_thread(resize, src, w) if src else None
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = "%s.%d.tmp" % (p, os.getpid())
        with open(tmp, "wb") as f:
            f.write(out or b"")                           # remembered either way: no second fetch to learn it
        os.replace(tmp, p)
        _writes += 1
        if _writes % 200 == 0:
            await asyncio.to_thread(_prune)
    except OSError as e:
        logger.debug("[thumb] cache write failed: %s", e)
    return out

