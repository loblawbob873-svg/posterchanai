"""Album covers for the Music library, looked up ONLINE on a person's request (opt-in, per user, off by default).

The Music app asks THIS node for an album it has no cover for (the files carry none); the node asks MusicBrainz which
release group that is, then the Cover Art Archive for its front cover, and caches the bytes keyed by the MusicBrainz
id -- so a cover is fetched from the internet once, ever, however many people or devices ask for it.

Rules, each one a failure this module was written to avoid:
  * OUTBOUND THROUGH THE NODE'S PROXY FALLBACK (Tor first, then direct: proxy_utils.afallback_transport), the way
    every other third-party request from this app goes -- a person's library is nobody's business but theirs.
  * MUSICBRAINZ ASKS FOR <= 1 REQUEST A SECOND and a meaningful User-Agent; both are enforced here, for the whole
    process, under concurrency (one lock, one clock). Breaking it gets the node's address blocked.
  * "COULD NOT ASK" IS NOT "NO COVER". A timeout, a refused connection, a 5xx or a 429 is `unavailable` and is never
    cached: caching it would answer "there is no cover" for a week to an album that has one.
  * WHAT COMES BACK IS CHECKED: image/* only, at most MAX_BYTES (read as a stream, so a huge answer is refused
    without being held), and it must decode (Pillow) -- the bytes are served to browsers from this origin.
  * THE ARTIST AND ALBUM ARE NEVER LOGGED (they are what somebody listens to). Outcomes and sizes only. On disk the
    query is a hash; the cache holds covers by MBID, which names an album and nothing about who asked.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

MB_BASE = "https://musicbrainz.org/ws/2"
CAA_BASE = "https://coverartarchive.org"
USER_AGENT = "PosterChanAI-Music/1.0 ( https://poster.place )"
MB_MIN_INTERVAL = 1.0          # seconds between MusicBrainz requests, process-wide
DEADLINE = 25.0                # one lookup, start to finish
MAX_BYTES = 2 * 1024 * 1024
NEGATIVE_TTL = 7 * 86400       # "there is no cover" is believed for a week
MIN_SCORE = 85                 # MusicBrainz search score below which a match is not trusted
# The Cover Art Archive answers with a redirect to the Internet Archive; nothing else is followed.
_REDIRECT_HOSTS = ("coverartarchive.org", "archive.org")
_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_IMAGE_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}


class _RedactSearch(logging.Filter):
    """httpx logs every request URL at INFO ("HTTP Request: GET https://musicbrainz.org/ws/2/...?query=..."), and the
    app's root logger is at INFO -- so without this the artist and album went into the journal on every lookup.
    The search's query string is cut from those lines; the line itself (method, host, status) stays."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:      # noqa: BLE001 -- a malformed record is not ours to break
            return True
        if "musicbrainz.org" in msg and "?" in msg:
            record.msg = re.sub(r"(musicbrainz\.org[^\s\"?]*)\?[^\s\"]*", r"\1?<redacted>", msg)
            record.args = ()
        return True


_redact = _RedactSearch()
if not any(isinstance(f, _RedactSearch) for f in logging.getLogger("httpx").filters):
    logging.getLogger("httpx").addFilter(_redact)


class Unavailable(Exception):
    """The cover service could not be asked (network, Tor, timeout, 5xx/429). Never 'no cover'."""


@dataclass
class Lookup:
    kind: str           # 'ok' | 'none' | 'unavailable'
    id: str = ""


def _root() -> Path:
    base = os.environ.get("PC_MUSIC_COVER_DIR") or str(Path(__file__).resolve().parents[2] / "data" / "music-covers")
    return Path(base)


def _transport() -> httpx.AsyncBaseTransport:
    """The outbound transport -- the node's proxy (Tor) first, then direct. Tests replace this."""
    from app.services.proxy_utils import afallback_transport
    return afallback_transport()


def valid_id(s: str) -> bool:
    return bool(_ID.match(str(s or "").lower()))


def _norm(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", str(s or "")).lower().split())


def _qhash(artist: str, album: str) -> str:
    return hashlib.sha256((_norm(artist) + "\n" + _norm(album)).encode()).hexdigest()


def _lucene(s: str) -> str:
    """A phrase for MusicBrainz's Lucene search: quotes and backslashes escaped, nothing else interpreted."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------- the cache (atomic writes; a torn file is a miss)
def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _cached_map(qh: str) -> str | None:
    """'<mbid>', '-' (no cover, still fresh) or None (never asked, or the negative answer expired)."""
    p = _root() / "map" / qh
    try:
        val = p.read_text().strip()
        if val == "-" and time.time() - p.stat().st_mtime > NEGATIVE_TTL:
            return None
        return val if (val == "-" or valid_id(val)) else None
    except OSError:
        return None


def _cached_cover(mbid: str) -> tuple[bytes, str] | None:
    d = _root() / "covers"
    try:
        return (d / f"{mbid}.img").read_bytes(), (d / f"{mbid}.mime").read_text().strip() or "image/jpeg"
    except OSError:
        return None


# ---------------------------------------------------------------- MusicBrainz, one request a second
_mb_locks: dict = {}       # one lock per event loop (a lock is bound to the loop that first awaits it)
_mb_last = 0.0             # ...but ONE clock for the process: the 1 req/s promise is about this node's address
_upstream_calls = {"mb": 0, "caa": 0}      # for the tests that prove a cache hit asks nobody


def _mb_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _mb_locks.get(id(loop))
    if lock is None:
        _mb_locks.clear()                  # loops come and go in tests; the app has one
        lock = _mb_locks[id(loop)] = asyncio.Lock()
    return lock


async def _mb_get(client: httpx.AsyncClient, path: str, params: dict) -> dict:
    global _mb_last
    async with _mb_lock():
        wait = MB_MIN_INTERVAL - (time.monotonic() - _mb_last)
        if wait > 0:
            await asyncio.sleep(wait)
        _mb_last = time.monotonic()
        _upstream_calls["mb"] += 1
        try:
            r = await client.get(MB_BASE + path, params=params)
        except httpx.HTTPError as e:
            raise Unavailable(type(e).__name__) from e
        finally:
            _mb_last = time.monotonic()
    if r.status_code == 429 or r.status_code >= 500:
        raise Unavailable(f"musicbrainz {r.status_code}")
    if r.status_code != 200:
        raise Unavailable(f"musicbrainz {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise Unavailable("musicbrainz answered something that is not JSON") from e


async def _release_group(client: httpx.AsyncClient, artist: str, album: str) -> str | None:
    q = f"releasegroup:{_lucene(album)} AND artist:{_lucene(artist)}"
    j = await _mb_get(client, "/release-group/", {"query": q, "fmt": "json", "limit": "5"})
    for rg in (j.get("release-groups") or []):
        try:
            score = int(rg.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        rid = str(rg.get("id") or "").lower()
        if score >= MIN_SCORE and valid_id(rid):
            return rid
    return None


# ---------------------------------------------------------------- the Cover Art Archive
class _Refused(Exception):
    """An answer that is not an acceptable cover (too big, not an image, does not decode)."""


async def _caa_front(client: httpx.AsyncClient, mbid: str) -> tuple[bytes, str] | None:
    """The front cover's bytes, None when the archive has no front cover (404). Raises Unavailable / _Refused."""
    url = f"{CAA_BASE}/release-group/{mbid}/front-500"
    for _ in range(5):
        _upstream_calls["caa"] += 1
        try:
            async with client.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    nxt = r.headers.get("location") or ""
                    nxt = str(httpx.URL(url).join(nxt))
                    host = (urlsplit(nxt).hostname or "").lower()
                    if urlsplit(nxt).scheme != "https" or not any(host == h or host.endswith("." + h) for h in _REDIRECT_HOSTS):
                        raise _Refused("redirect off the archive")
                    url = nxt
                    continue
                if r.status_code == 404:
                    return None
                if r.status_code == 429 or r.status_code >= 500:
                    raise Unavailable(f"cover archive {r.status_code}")
                if r.status_code != 200:
                    raise Unavailable(f"cover archive {r.status_code}")
                ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                if not ctype.startswith("image/"):
                    raise _Refused("not an image")
                declared = r.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                    raise _Refused("too large")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise _Refused("too large")
        except httpx.HTTPError as e:
            raise Unavailable(type(e).__name__) from e
        return _checked(bytes(buf))
    raise _Refused("too many redirects")


def _checked(data: bytes) -> tuple[bytes, str]:
    """Decodes as one of the formats a browser shows, or it is refused."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            im.verify()
    except Exception as e:      # noqa: BLE001 -- Pillow raises many types for bad input
        raise _Refused("does not decode") from e
    mime = _IMAGE_FORMATS.get(fmt or "")
    if not mime:
        raise _Refused("not a web image")
    return data, mime


def _store_cover(mbid: str, data: bytes, mime: str) -> None:
    d = _root() / "covers"
    _write(d / f"{mbid}.img", data)
    _write(d / f"{mbid}.mime", mime.encode())


# ---------------------------------------------------------------- the two questions the router asks
async def lookup(artist: str, album: str) -> Lookup:
    """Which cover (by MusicBrainz id) an album has: 'ok' + id, 'none', or 'unavailable' (never cached)."""
    qh = _qhash(artist, album)
    hit = _cached_map(qh)
    if hit == "-":
        logger.info("music cover lookup: none (cached)")
        return Lookup("none")
    if hit and _cached_cover(hit):
        logger.info("music cover lookup: ok (cached)")
        return Lookup("ok", hit)
    try:
        async with asyncio.timeout(DEADLINE):
            async with httpx.AsyncClient(transport=_transport(), timeout=12.0, follow_redirects=False,
                                         headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
                mbid = hit or await _release_group(client, artist, album)
                if not mbid:
                    _write(_root() / "map" / qh, b"-")
                    logger.info("music cover lookup: none (no matching release group)")
                    return Lookup("none")
                try:
                    got = await _caa_front(client, mbid)
                except _Refused as e:
                    _write(_root() / "map" / qh, b"-")
                    logger.info("music cover lookup: none (refused: %s)", e)
                    return Lookup("none")
                if not got:
                    _write(_root() / "map" / qh, b"-")
                    logger.info("music cover lookup: none (the archive has no front cover)")
                    return Lookup("none")
                _store_cover(mbid, *got)
                _write(_root() / "map" / qh, mbid.encode())
                logger.info("music cover lookup: ok (%d bytes)", len(got[0]))
                return Lookup("ok", mbid)
    except (Unavailable, TimeoutError) as e:
        logger.info("music cover lookup: unavailable (%s)", type(e).__name__ if isinstance(e, TimeoutError) else e)
        return Lookup("unavailable")


async def cover(mbid: str) -> tuple[str, bytes, str]:
    """('ok', bytes, mime) | ('none', b'', '') | ('unavailable', b'', ''). Fetches from the archive when the cache
    lost it -- by id, so this never asks MusicBrainz anything."""
    mbid = str(mbid or "").lower()
    if not valid_id(mbid):
        return "none", b"", ""
    hit = _cached_cover(mbid)
    if hit:
        return "ok", hit[0], hit[1]
    try:
        async with asyncio.timeout(DEADLINE):
            async with httpx.AsyncClient(transport=_transport(), timeout=12.0, follow_redirects=False,
                                         headers={"User-Agent": USER_AGENT}) as client:
                try:
                    got = await _caa_front(client, mbid)
                except _Refused as e:
                    logger.info("music cover fetch: none (refused: %s)", e)
                    return "none", b"", ""
    except (Unavailable, TimeoutError) as e:
        logger.info("music cover fetch: unavailable (%s)", type(e).__name__ if isinstance(e, TimeoutError) else e)
        return "unavailable", b"", ""
    if not got:
        return "none", b"", ""
    _store_cover(mbid, *got)
    logger.info("music cover fetch: ok (%d bytes)", len(got[0]))
    return "ok", got[0], got[1]
