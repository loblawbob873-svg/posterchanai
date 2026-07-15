"""RSS/Atom feed reader for the built-in News view.

Server-side because the browser can't fetch arbitrary feeds (CORS) and we want the built-in HTTP
proxy (→ Tor) with a DIRECT fallback. Multi-user efficient: each feed URL is fetched at most once per
_TTL and served from a SHARED in-process cache, and concurrent requests for the same URL ride ONE
upstream fetch (in-flight dedup) — 100 users viewing the same feed cost one request. No DB, no per-user
state here (a user's feed list + read state live as Nostr events on the client).
"""
import asyncio
import ipaddress
import logging
import re
import socket
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlparse

import httpx
from defusedxml.ElementTree import fromstring as _xml_fromstring   # entity-expansion/XXE-safe parse of untrusted feeds

from app.services.proxy_utils import afallback_transport

logger = logging.getLogger(__name__)

# Tor Browser standard UA (shared by all Tor users → no fingerprint); omit brotli (httpx won't decode br).
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Accept": "application/rss+xml,application/atom+xml,application/xml,text/xml;q=0.9,text/html;q=0.8,*/*;q=0.7",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
}
_TTL = 300.0            # a feed is refetched at most once per 5 min; all users served from cache between
_MAX_BYTES = 4_000_000  # STREAM cap: stop reading the body past this (a huge/garbage response can't buffer)
_MAX_ITEMS = 50
_MAX_CACHE = 400        # bound the shared cache (public endpoint w/ arbitrary URLs) — evict oldest past this
_MAX_REDIRECTS = 5

_cache: dict = {}       # url -> (monotonic_fetched_at, payload)
_inflight: dict = {}    # url -> asyncio.Future  (dedup concurrent fetches into ONE upstream request)

_TAG = lambda t: t.rsplit('}', 1)[-1].lower() if '}' in t else t.lower()
_HTML_TAGS = re.compile(r'<[^>]+>')
_WS = re.compile(r'\s+')
_IMG_SRC = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def looks_fetchable(url: str) -> bool:
    """CHEAP syntactic gate (NO DNS) for the request path — rejects non-http, empty host, localhost and
    private/loopback IP LITERALS. The authoritative resolve-based check (is_safe_host, run per redirect hop
    at FETCH time) is what actually blocks SSRF, so a cache HIT costs zero DNS lookups — this is what lets
    it scale to many users without a getaddrinfo per request. A hostname that resolves to a private IP still
    passes here but is rejected by is_safe_host when the (cache-missing) fetch actually happens."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".lan")):
        return False
    try:
        ip = ipaddress.ip_address(host)   # only if it's an IP literal
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    except ValueError:
        pass   # hostname → resolve-based is_safe_host runs at fetch time
    return True


def is_safe_host(url: str) -> bool:
    """SSRF guard: reject localhost / private / reserved targets. Resolves the hostname (blocking — call
    via run_in_threadpool) so a public name that maps to an internal IP is also rejected. The DIRECT
    fallback is the risk here (the proxy only reaches external hosts)."""
    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".lan")):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False   # unresolvable → reject
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def _snippet(html_text: str, n: int = 240) -> str:
    if not html_text:
        return ""
    txt = _WS.sub(" ", unescape(_HTML_TAGS.sub(" ", html_text))).strip()
    return (txt[:n] + "…") if len(txt) > n else txt


def _img_from_html(html_text: str) -> str:
    m = _IMG_SRC.search(html_text or "")
    return m.group(1) if m else ""


def _to_ts(s: str) -> int:
    s = (s or "").strip()
    if not s:
        return 0
    try:
        return int(parsedate_to_datetime(s).timestamp())   # RFC-822 (RSS <pubDate>)
    except Exception:
        pass
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())   # ISO-8601 (Atom)
    except Exception:
        return 0


def _abs_url(link: str, base: str) -> str:
    if not link:
        return ""
    if link.startswith("//"):
        return "https:" + link
    if link.startswith("/"):
        return base.rstrip("/") + link
    return link


def parse_feed(raw: bytes, base_url: str) -> tuple:
    """Parse RSS or Atom bytes → (feed_title, items). Each item: {id,title,link,ts,snippet,image}."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    root = _xml_fromstring(raw)   # defusedxml: blocks billion-laughs / external-entity attacks

    feed_title = ""
    for el in root.iter():   # first channel/feed-level <title> (before any item)
        t = _TAG(el.tag)
        if t in ("item", "entry"):
            break
        if t == "title" and (el.text or "").strip():
            feed_title = el.text.strip()
            break

    items, seen = [], set()
    for node in root.iter():
        if _TAG(node.tag) not in ("item", "entry"):
            continue
        title = link = guid = date = desc = image = ""
        for ch in node:
            tag = _TAG(ch.tag)
            url_attr = (ch.get("url") or "").strip()
            ctype = ch.get("type") or ""
            if tag == "title" and not title:
                title = (ch.text or "").strip()
            elif tag == "link" and not link:
                link = (ch.get("href") or (ch.text or "")).strip()
            elif tag in ("guid", "id") and not guid:
                guid = (ch.text or "").strip()
            elif tag in ("pubdate", "published", "updated", "date") and not date:
                date = (ch.text or "").strip()
            elif tag == "enclosure" and url_attr and "image" in ctype and not image:
                image = url_attr
            elif tag == "thumbnail" and url_attr and not image:           # media:thumbnail
                image = url_attr
            elif tag == "content" and url_attr and "image" in ctype and not image:  # media:content image
                image = url_attr
            elif tag in ("description", "summary", "encoded") and not desc:
                desc = ch.text or ""
            elif tag == "content" and not url_attr and not desc:          # Atom <content> body
                desc = ch.text or ""

        title = unescape(title)
        link = _abs_url(link, base_url)
        if not link and guid.startswith("http"):
            link = guid
        if not (title and link):
            continue
        key = guid or link
        if key in seen:
            continue
        seen.add(key)
        # Fallback: some feeds NEST the thumbnail/description one level down (e.g. YouTube's
        # <media:group><media:thumbnail url=.../><media:description>… — the direct-children loop misses it).
        # Scan descendants for a media:thumbnail / image media:content and a description.
        if not image or not desc:
            for sub in node.iter():
                st = _TAG(sub.tag)
                u = (sub.get("url") or "").strip()
                if not image and u and (st == "thumbnail" or (st == "content" and "image" in (sub.get("type") or ""))):
                    image = u
                if not desc and st in ("description", "summary") and (sub.text or "").strip():
                    desc = sub.text
                if image and desc:
                    break
        if not image:
            image = _abs_url(_img_from_html(desc), base_url)
        items.append({"id": key, "title": title, "link": link, "ts": _to_ts(date),
                      "snippet": _snippet(desc), "image": image})
        if len(items) >= _MAX_ITEMS:
            break

    items.sort(key=lambda x: x["ts"], reverse=True)
    return feed_title, items


async def _get_bytes(url: str) -> bytes:
    """Fetch bytes, following redirects MANUALLY so each hop's host is re-validated with is_safe_host —
    closing the SSRF-via-redirect bypass (a feed that 302s to an internal host would otherwise be fetched
    through the direct fallback). Streams the body and stops past _MAX_BYTES so a huge response can't buffer."""
    loop = asyncio.get_event_loop()
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False,
                                 transport=afallback_transport()) as client:   # proxy(Tor)→direct
        cur = url
        for _ in range(_MAX_REDIRECTS + 1):
            if not await loop.run_in_executor(None, is_safe_host, cur):
                raise ValueError("disallowed host")
            async with client.stream("GET", cur, headers=_HEADERS) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    cur = str(httpx.URL(cur).join(r.headers["location"]))
                    continue
                r.raise_for_status()
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > _MAX_BYTES:
                        break
                return bytes(buf[:_MAX_BYTES])
        raise ValueError("too many redirects")


async def _fetch(url: str) -> dict:
    """One upstream fetch+parse. Never raises — returns an error payload instead."""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    try:
        raw = await _get_bytes(url)
        title, items = parse_feed(raw, base)
        return {"url": url, "title": title or parsed.netloc, "items": items, "error": None}
    except Exception as e:
        logger.info("[rss] fetch failed for %s: %s", url, e)
        return {"url": url, "title": parsed.netloc, "items": [], "error": str(e)}


def _evict():
    """Bound the shared cache — drop the oldest entries once past _MAX_CACHE (public endpoint, arbitrary URLs)."""
    if len(_cache) <= _MAX_CACHE:
        return
    for k in sorted(_cache, key=lambda k: _cache[k][0])[: len(_cache) - _MAX_CACHE]:
        _cache.pop(k, None)


def _start_fetch(url: str):
    """Return the in-flight fetch future for `url` (created if needed). It always resolves to a payload dict.
    A done-callback caches a successful result and clears the in-flight slot — so on-demand awaiters AND a
    fire-and-forget background refresh share ONE upstream request, and both update the cache the same way."""
    fut = _inflight.get(url)
    if fut is None:
        fut = asyncio.ensure_future(_fetch(url))
        _inflight[url] = fut
        def _done(f, u=url):
            _inflight.pop(u, None)
            try:
                p = f.result()
                if isinstance(p, dict) and not p.get("error"):
                    _cache[u] = (time.monotonic(), p)
                    _evict()
            except Exception:
                pass
        fut.add_done_callback(_done)
    return fut


async def get_feed(url: str, force: bool = False) -> dict:
    """Steady stale-while-revalidate read from the shared cache:
       fresh (< _TTL)  → return it;  stale → return it NOW + refresh in the background;
       cold / force    → fetch once (concurrent misses ride a single in-flight request).
    The resolve-based SSRF check runs inside the fetch (is_safe_host per redirect hop), so a cache read does
    NO DNS — this is what keeps it CPU-cheap and easy on the source sites (one fetch per feed per TTL, no
    matter how many users)."""
    now = time.monotonic()
    hit = _cache.get(url)
    if hit and not force:
        if (now - hit[0]) < _TTL:
            return {**hit[1], "cached": True}
        _start_fetch(url)                                    # stale → serve now, revalidate in background
        return {**hit[1], "cached": True, "stale": True}

    payload = await _start_fetch(url)                        # cold/forced → fetch (deduped); callback caches
    if payload.get("error") and hit:                         # transient failure → serve the last good copy
        return {**hit[1], "cached": True, "stale": True}
    return {**payload, "cached": False}
