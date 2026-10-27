"""Album covers for the Music library, looked up ONLINE on a person's request (opt-in, per user, off by default).

The Music app asks THIS node for an album it has no cover for (the files carry none); the node asks MusicBrainz which
release group that is, then the Cover Art Archive for its front cover -- and KEEPS IT ON THIS NODE'S BLOSSOM, so a
cover is fetched from the internet once, ever, however many people or devices ask for it.

WHERE IT IS KEPT. The bytes are a Blossom blob (content-addressed, owned by the node's operator key, `keep` -- never
aged out -- and not mirrored), and the two answers that decide whether the internet is asked live on this node's
relay as operator documents: `pcai:musiccover:<mbid>` -> {sha, mime} and `pcai:musiccoverq:<query hash>` -> {id, t}
(the release group, or "-" for "no cover", believed for NEGATIVE_TTL). They used to be files under data/music-covers,
a private folder no backup, node check or Blossom tooling knew about; a cache found there is moved into Blossom the
first time it is read. "Could not ask" the relay or the store is `unavailable` -- never a reason to go online.

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
    """The OLD cache folder -- read only to move what it holds into Blossom (see _LegacyFiles)."""
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


# ---------------------------------------------------------------- where covers are kept
class _Keep:
    """The cover store: Blossom for the bytes, the relay for the two maps. Every method may raise Unavailable."""
    COVER, QUERY = "pcai:musiccover:", "pcai:musiccoverq:"

    @staticmethod
    def _port() -> int:
        from app.services import settings_store
        return settings_store.get_int("nostr_relay_port", 3052)

    @staticmethod
    async def _with_db(fn):
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            return await fn(db)
        finally:
            db.close()

    async def _sk(self) -> bytes:
        from app.services import settings_store

        async def go(db):
            return settings_store._operator_seckey(db)
        sk = await self._with_db(go)
        if not sk:
            raise Unavailable("no operator key")
        return sk

    async def _get(self, d: str):
        from app.services import nostr_store
        try:
            return await nostr_store.get_doc(self._port(), d, seckey=await self._sk(), strict=True)
        except Unavailable:
            raise
        except Exception as e:      # noqa: BLE001 -- an unreadable relay is "could not ask", whatever it raised
            raise Unavailable("relay: %s" % type(e).__name__) from e

    async def _put(self, d: str, doc: dict) -> None:
        from app.services import nostr_store
        if not await nostr_store.put_doc(self._port(), await self._sk(), d, doc):
            raise Unavailable("relay refused the write")

    async def get_map(self, qh: str):
        doc = await self._get(self.QUERY + qh)
        return doc if isinstance(doc, dict) else None

    async def put_map(self, qh: str, mbid: str, t: int | None = None) -> None:
        await self._put(self.QUERY + qh, {"id": mbid, "t": int(t if t is not None else time.time())})

    async def get_cover(self, mbid: str):
        doc = await self._get(self.COVER + mbid)
        sha = str((doc or {}).get("sha") or "") if isinstance(doc, dict) else ""
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            return None
        from app.services import blob_index, blossom_service
        try:
            blob = await blob_index.aget(sha)
        except Exception as e:      # noqa: BLE001
            raise Unavailable("blob index: %s" % type(e).__name__) from e
        if blob is None:
            return None             # the record outlived its bytes: fetched again, by id
        data = await self._with_db(lambda db: blossom_service.read_full(db, blob))
        if data is None:
            raise Unavailable("the store could not read the cover")
        return data, str(doc.get("mime") or "image/jpeg")

    async def put_cover(self, mbid: str, data: bytes, mime: str) -> None:
        from app.services import blossom_service
        from app.services.nostr import bip340
        pk = bip340.pubkey_from_seckey(await self._sk()).hex()
        desc = await self._with_db(lambda db: blossom_service.save_blob(
            db, pk, data, mime, mirror=False, keep=True, filename="cover-%s.%s" % (mbid, blossom_service.ext_for_mime(mime).lstrip("."))))
        await self._put(self.COVER + mbid, {"sha": desc["sha256"], "mime": mime})


class _LegacyFiles:
    """The folder covers used to be kept in: read once, moved into the store, then deleted."""

    @staticmethod
    def map(qh: str):
        p = _root() / "map" / qh
        try:
            return p.read_text().strip(), int(p.stat().st_mtime)
        except OSError:
            return None

    @staticmethod
    def cover(mbid: str):
        d = _root() / "covers"
        try:
            return (d / f"{mbid}.img").read_bytes(), (d / f"{mbid}.mime").read_text().strip() or "image/jpeg"
        except OSError:
            return None

    @staticmethod
    def drop(*paths) -> None:
        for p in paths:
            try:
                p.unlink()
            except OSError:
                pass


_keep = _Keep()


async def _cached_map(qh: str) -> str | None:
    """'<mbid>', '-' (no cover, still fresh) or None (never asked, or the negative answer expired)."""
    doc = await _keep.get_map(qh)
    if doc is None:
        old = _LegacyFiles.map(qh)
        if old and (old[0] == "-" or valid_id(old[0])):
            await _keep.put_map(qh, old[0], old[1])   # the old answer's age: "no cover" is believed for a week from THEN
            doc = {"id": old[0], "t": old[1]}
            _LegacyFiles.drop(_root() / "map" / qh)
    if not doc:
        return None
    val = str(doc.get("id") or "")
    if val == "-" and time.time() - int(doc.get("t") or 0) > NEGATIVE_TTL:
        return None
    return val if (val == "-" or valid_id(val)) else None


async def _cached_cover(mbid: str) -> tuple[bytes, str] | None:
    hit = await _keep.get_cover(mbid)
    if hit is None:
        old = _LegacyFiles.cover(mbid)
        if old:
            await _keep.put_cover(mbid, *old)
            _LegacyFiles.drop(_root() / "covers" / f"{mbid}.img", _root() / "covers" / f"{mbid}.mime")
            hit = old
    return hit


async def _remember(qh: str, mbid: str) -> None:
    """A write after the answer is known: if the store will not take it, the answer still goes back (and is
    simply asked for again next time) -- an answer in hand is never turned into "could not ask"."""
    try:
        await _keep.put_map(qh, mbid)
    except Unavailable as e:
        logger.info("music cover lookup: answer not kept (%s)", e)


async def _keep_cover(mbid: str, data: bytes, mime: str) -> bool:
    try:
        await _keep.put_cover(mbid, data, mime)
        return True
    except Unavailable as e:
        logger.info("music cover: bytes not kept (%s)", e)
        return False


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


# ---------------------------------------------------------------- the two questions the router asks
async def lookup(artist: str, album: str) -> Lookup:
    """Which cover (by MusicBrainz id) an album has: 'ok' + id, 'none', or 'unavailable' (never cached)."""
    qh = _qhash(artist, album)
    try:
        hit = await _cached_map(qh)
        if hit == "-":
            logger.info("music cover lookup: none (cached)")
            return Lookup("none")
        if hit and await _cached_cover(hit):
            logger.info("music cover lookup: ok (cached)")
            return Lookup("ok", hit)
    except Unavailable as e:
        logger.info("music cover lookup: unavailable (the store: %s)", e)
        return Lookup("unavailable")
    try:
        async with asyncio.timeout(DEADLINE):
            async with httpx.AsyncClient(transport=_transport(), timeout=12.0, follow_redirects=False,
                                         headers={"User-Agent": USER_AGENT, "Accept": "application/json"}) as client:
                mbid = hit or await _release_group(client, artist, album)
                if not mbid:
                    await _remember(qh, "-")
                    logger.info("music cover lookup: none (no matching release group)")
                    return Lookup("none")
                try:
                    got = await _caa_front(client, mbid)
                except _Refused as e:
                    await _remember(qh, "-")
                    logger.info("music cover lookup: none (refused: %s)", e)
                    return Lookup("none")
                if not got:
                    await _remember(qh, "-")
                    logger.info("music cover lookup: none (the archive has no front cover)")
                    return Lookup("none")
                await _keep_cover(mbid, *got)
                await _remember(qh, mbid)      # even if the bytes were not kept: the id still skips MusicBrainz
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
    try:
        hit = await _cached_cover(mbid)
    except Unavailable as e:
        logger.info("music cover fetch: unavailable (the store: %s)", e)
        return "unavailable", b"", ""
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
    await _keep_cover(mbid, *got)       # served even if not kept -- the bytes are good
    logger.info("music cover fetch: ok (%d bytes)", len(got[0]))
    return "ok", got[0], got[1]
