"""Torrent RSS feeds — subscribe to a feed, poll it, add what matches.

A torrent RSS feed (showRSS, nyaa, a Jackett/torznab endpoint, a private tracker's personal feed)
is a list of releases. This service keeps the subscriptions, polls them on a timer in the WORKER
process, and hands anything new and matching to this node's torrent client.

THREE THINGS ARE DELIBERATE HERE:

1. **The SSRF guard is the news reader's, not a second one.** `rss_service.looks_fetchable` /
   `is_safe_host` already reject localhost, private/reserved literals and public names that RESOLVE
   to an internal address — and `rss_service._get_bytes` re-runs `is_safe_host` on EVERY REDIRECT
   HOP, which is the half a hand-rolled check always forgets. A feed URL is user-supplied and this
   node fetches it with its own network position, so it goes through exactly that code.

2. **A feed is not a firehose.** Without a filter, subscribing to a category feed means downloading
   the category. `include`/`exclude` are matched against the item TITLE; an empty `include` means
   "everything", which is why `_max_per_poll` exists as the other bound — the first poll of a
   200-item feed must not enqueue 200 torrents.

3. **The first poll of a NEW feed adds nothing.** It marks every item as seen and stops. A feed is
   a window onto a backlog, and "subscribe" means "tell me what happens NEXT" — the alternative
   (which the fediverse bridge learned the same way) is that adding a feed silently starts
   downloading its entire history. Use "Check now" on the feed to see what it holds and pick from it.

State lives in ONE json file next to the torrent client's own resume data, read and written under an
`flock`: the feeds are edited by the APP process and polled by the WORKER, two processes writing one
file, and a torn read here reads as "no feeds" — which would re-add a whole backlog on the next poll.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from defusedxml.ElementTree import fromstring as _xml_fromstring   # entity-expansion/XXE-safe

from app.services import rss_service, settings_store

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_MIN = 30
_DEFAULT_MAX_PER_POLL = 5
_MAX_FEEDS = 50
_MAX_SEEN_PER_FEED = 500      # bounded: a feed that never repeats an id must not grow the file forever
_BTIH = re.compile(r"^([0-9a-fA-F]{40}|[0-9a-fA-F]{64}|[A-Z2-7]{32})$")


# --------------------------------------------------------------------------- state file

def _state_dir() -> Path:
    """Beside the torrent client's own resume data, so a node's torrent state is in one place."""
    base = settings_store.get("bt_download_dir") or "/var/lib/posterchanai/torrents"
    return Path(base) / ".resume"


def _store_path() -> Path:
    return _state_dir() / "torrent_rss.json"


def _read_locked(fh) -> dict:
    fh.seek(0)
    raw = fh.read()
    if not raw.strip():
        return {"feeds": [], "seen": {}}
    try:
        data = json.loads(raw)
    except Exception as e:
        # A corrupt file is NOT "no feeds": answering empty would drop every subscription and then
        # rewrite the file with that answer. Raise instead and let the caller report it.
        raise ValueError(f"torrent_rss.json is unreadable: {e}")
    if not isinstance(data, dict):
        raise ValueError("torrent_rss.json is not an object")
    data.setdefault("feeds", [])
    data.setdefault("seen", {})
    return data


def _with_store(fn):
    """Read-modify-write the store under an exclusive flock. `fn(data)` returns the data to write,
    or None to leave the file untouched; its return value is passed back to the caller as `result`
    via the attribute set on it. Cross-process safe: the app edits feeds, the worker records seen."""
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # "a+" creates without truncating; the flock is held for the whole read-modify-write.
    with open(path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            data = _read_locked(fh)
            out = fn(data)
            if out is not None:
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(json.dumps(out, indent=1))
                os.replace(tmp, path)
            return out
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _read_store() -> dict:
    try:
        holder = {}

        def _grab(data):
            holder["d"] = data
            return None

        _with_store(_grab)
        return holder.get("d", {"feeds": [], "seen": {}})
    except FileNotFoundError:
        return {"feeds": [], "seen": {}}


def _normalize(url: str) -> str:
    """`rss_service.normalize_url` prepends https:// to anything that does not already start with
    http(s) — which is right for "example.com/rss" typed by hand, and WRONG for a URL that carries
    its own scheme: "file:///etc/passwd" becomes "https://file:///etc/passwd", whose host is the
    word `file`, and that passes looks_fetchable. So refuse a foreign scheme BEFORE normalizing.
    This is not a second SSRF check; it is the gap normalize_url opens in the first one."""
    u = (url or "").strip()
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):", u)
    if m and m.group(1).lower() not in ("http", "https"):
        raise ValueError("only http:// and https:// feed URLs are supported")
    return rss_service.normalize_url(u)


# --------------------------------------------------------------------------- feed CRUD

def _public(feed: dict, seen_count: int = 0) -> dict:
    return {
        "id": feed.get("id", ""),
        "url": feed.get("url", ""),
        "title": feed.get("title", "") or urlparse(feed.get("url", "")).netloc,
        "include": feed.get("include", ""),
        "exclude": feed.get("exclude", ""),
        "enabled": bool(feed.get("enabled", True)),
        "added_at": int(feed.get("added_at", 0)),
        "last_check": int(feed.get("last_check", 0)),
        "last_error": feed.get("last_error", "") or "",
        "last_added": int(feed.get("last_added", 0)),
        "added_total": int(feed.get("added_total", 0)),
        # "primed" is load-bearing in the UI: until it is true the feed has added nothing and is
        # only learning its backlog, and a feed that says "0 added" for that reason must not read
        # the same as one whose filter matches nothing.
        "primed": bool(feed.get("primed", False)),
        "seen": int(seen_count),
    }


def list_feeds() -> list[dict]:
    data = _read_store()
    seen = data.get("seen", {}) or {}
    return [_public(f, len(seen.get(f.get("id", ""), []))) for f in data.get("feeds", [])]


def add_feed(url: str, title: str = "", include: str = "", exclude: str = "",
             enabled: bool = True) -> dict:
    """Add a subscription. Raises ValueError for a URL the SSRF guard refuses or a duplicate."""
    url = _normalize(url)
    if not rss_service.looks_fetchable(url):
        raise ValueError("that URL can't be fetched (http/https only, and no private or local hosts)")

    created = {}

    def _mut(data):
        feeds = data.get("feeds", [])
        if any((f.get("url") or "").rstrip("/") == url.rstrip("/") for f in feeds):
            raise ValueError("that feed is already subscribed")
        if len(feeds) >= _MAX_FEEDS:
            raise ValueError(f"too many feeds (limit {_MAX_FEEDS})")
        feed = {
            "id": secrets.token_hex(8),
            "url": url,
            "title": (title or "").strip(),
            "include": (include or "").strip(),
            "exclude": (exclude or "").strip(),
            "enabled": bool(enabled),
            "added_at": int(time.time()),
            "last_check": 0,
            "last_error": "",
            "last_added": 0,
            "added_total": 0,
            # A NEW feed has not had its backlog marked seen yet. The first poll does that and adds
            # nothing — see the module docstring.
            "primed": False,
        }
        feeds.append(feed)
        data["feeds"] = feeds
        created.update(feed)
        return data

    _with_store(_mut)
    return _public(created)


def update_feed(feed_id: str, **fields) -> dict:
    out = {}

    def _mut(data):
        for f in data.get("feeds", []):
            if f.get("id") == feed_id:
                for k in ("title", "include", "exclude"):
                    if k in fields and fields[k] is not None:
                        f[k] = str(fields[k]).strip()
                if fields.get("enabled") is not None:
                    f["enabled"] = bool(fields["enabled"])
                if fields.get("url"):
                    u = _normalize(str(fields["url"]))
                    if not rss_service.looks_fetchable(u):
                        raise ValueError("that URL can't be fetched (http/https only, and no private or local hosts)")
                    f["url"] = u
                out.update(f)
                return data
        raise ValueError("no such feed")

    _with_store(_mut)
    return _public(out)


def remove_feed(feed_id: str) -> bool:
    hit = {"n": 0}

    def _mut(data):
        feeds = [f for f in data.get("feeds", []) if f.get("id") != feed_id]
        hit["n"] = len(data.get("feeds", [])) - len(feeds)
        if not hit["n"]:
            return None
        data["feeds"] = feeds
        data.get("seen", {}).pop(feed_id, None)
        return data

    _with_store(_mut)
    return hit["n"] > 0


def get_feed(feed_id: str) -> Optional[dict]:
    for f in _read_store().get("feeds", []):
        if f.get("id") == feed_id:
            return f
    return None


# --------------------------------------------------------------------------- title filters

def _terms(spec: str) -> list:
    """Split a filter into terms. A term wrapped in / / is a regular expression; anything else is a
    case-insensitive substring. Separated by newline or comma — a comma is what people type, a
    newline is what survives a paste."""
    out = []
    for raw in re.split(r"[\n,]", spec or ""):
        t = raw.strip()
        if not t:
            continue
        m = re.match(r"^/(.*)/$", t, re.S)
        if m and m.group(1):
            try:
                # Case-INSENSITIVE, like the substring form beside it: a release title's case is the
                # uploader's business, and /S01E/ silently matching nothing on "s01e04" is a filter
                # that looks correct and downloads nothing.
                out.append(re.compile(m.group(1), re.I))
                continue
            except re.error:
                pass   # an invalid regex falls back to a literal match rather than matching nothing
        out.append(t.lower())
    return out


def matches(title: str, include: str = "", exclude: str = "") -> bool:
    """Does this release title pass the feed's filters?

    include: ANY term matches (empty = everything passes). exclude: ANY term matching rejects.
    Exclude is checked LAST and wins — "1080p but not HDCAM" is the shape people actually want, and
    an include-wins order makes the exclude box do nothing on the one release it was typed for.
    """
    t = (title or "")
    low = t.lower()
    inc = _terms(include)
    if inc and not any((term.search(t) if hasattr(term, "search") else term in low) for term in inc):
        return False
    for term in _terms(exclude):
        if (term.search(t) if hasattr(term, "search") else term in low):
            return False
    return True


# --------------------------------------------------------------------------- feed parsing

def _local(tag: str) -> str:
    return (tag.rsplit("}", 1)[-1] if "}" in tag else tag).lower()


def _magnet_from_hash(ih: str, title: str = "") -> str:
    ih = (ih or "").strip()
    if not _BTIH.match(ih):
        return ""
    from urllib.parse import quote
    return f"magnet:?xt=urn:btih:{ih}" + (f"&dn={quote(title)}" if title else "")


def parse_torrent_feed(raw: bytes) -> list[dict]:
    """RSS/Atom bytes → torrent items: {id, title, magnet, torrent_url, link, ts, size}.

    This is NOT rss_service.parse_feed, and the difference is the point: that one is a NEWS reader
    and throws away every enclosure that is not an image, which on a torrent feed is the torrent.
    The magnet can be in the <link>, in an <enclosure url=…>, in a <guid>, in a torznab
    <torznab:attr name="magneturl">, or nowhere at all — with only an infohash
    (<nyaa:infoHash>, <torznab:attr name="infohash">) from which a magnet is built. So the whole
    item subtree is scanned for all of them rather than a fixed list of child tags.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    root = _xml_fromstring(raw)

    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        title = link = guid = date = magnet = torrent_url = infohash = ""
        size = 0
        for el in node.iter():
            tag = _local(el.tag)
            text = (el.text or "").strip()
            attrs = el.attrib or {}
            name = (attrs.get("name") or "").lower()
            # every place a URL can hide, in the order a feed is likely to put it
            cands = [text, attrs.get("url", ""), attrs.get("value", ""), attrs.get("href", "")]
            for c in cands:
                c = (c or "").strip()
                if not magnet and c.lower().startswith("magnet:?"):
                    magnet = c
                elif not torrent_url and c.lower().startswith(("http://", "https://")) and \
                        re.search(r"\.torrent(\?|$)", c, re.I):
                    torrent_url = c
            if tag == "title" and not title:
                title = text
            elif tag in ("guid", "id") and not guid:
                guid = text
            elif tag in ("pubdate", "published", "updated", "date") and not date:
                date = text
            elif tag == "link" and not link:
                link = (attrs.get("href") or text).strip()
            elif tag in ("infohash", "info_hash") and not infohash:
                infohash = text
            elif tag == "attr" and name == "infohash" and not infohash:
                infohash = (attrs.get("value") or "").strip()
            elif tag == "enclosure" and not torrent_url:
                u = (attrs.get("url") or "").strip()
                ctype = (attrs.get("type") or "").lower()
                if u.lower().startswith("magnet:?"):
                    magnet = magnet or u
                elif "bittorrent" in ctype and u.lower().startswith(("http://", "https://")):
                    torrent_url = u
                if not size:
                    try:
                        size = int(attrs.get("length") or 0)
                    except (TypeError, ValueError):
                        size = 0
            elif tag in ("size", "contentlength") and not size and text.isdigit():
                size = int(text)
            elif tag == "attr" and name == "size" and not size:
                v = (attrs.get("value") or "").strip()
                size = int(v) if v.isdigit() else 0

        if not magnet and infohash:
            magnet = _magnet_from_hash(infohash, title)
        if not magnet and guid.lower().startswith("magnet:?"):
            magnet = guid
        if not title or not (magnet or torrent_url):
            continue
        items.append({
            "id": guid or magnet or torrent_url or title,
            "title": title,
            "magnet": magnet,
            "torrent_url": torrent_url,
            "link": link or guid,
            "ts": rss_service._to_ts(date),
            "size": size,
        })
    return items


async def fetch_feed_items(url: str) -> list[dict]:
    """Fetch + parse one feed. The URL goes through the news reader's SSRF guard, and the fetch
    itself is rss_service._get_bytes, which re-validates the host on EVERY redirect hop."""
    url = _normalize(url)
    if not rss_service.looks_fetchable(url):
        raise ValueError("that URL can't be fetched (http/https only, and no private or local hosts)")
    raw = await rss_service._get_bytes(url)     # per-hop is_safe_host + size cap + proxy transport
    return parse_torrent_feed(raw)


# --------------------------------------------------------------------------- adding

async def add_one(magnet: str = "", torrent_url: str = "") -> str:
    """Hand one release to this node's torrent client (or to the remote one it forwards to).
    Returns the info hash, or raises."""
    server_url = settings_store.get("bt_server_url")
    if server_url:
        import httpx
        from app.utils import lb_auth
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{server_url.rstrip('/')}/api/torrent/add",
                                  headers=lb_auth.headers(),
                                  json={"magnet": magnet, "torrent_url": torrent_url})
            r.raise_for_status()
            return (r.json() or {}).get("info_hash", "")

    from app.routers.torrent import get_bt_service      # lazy: the router imports this module
    service = get_bt_service(None)
    if not service:
        raise RuntimeError("torrent client not configured")
    if magnet:
        return service.add_magnet(magnet)
    if torrent_url:
        import httpx
        from app.services.proxy_utils import afallback_transport
        if not rss_service.looks_fetchable(torrent_url):
            raise ValueError("refusing to fetch that .torrent URL")
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False,
                                     transport=afallback_transport()) as client:
            loop = asyncio.get_event_loop()
            cur, data = torrent_url, None
            for _ in range(4):
                if not await loop.run_in_executor(None, rss_service.is_safe_host, cur):
                    raise ValueError("disallowed host")
                r = await client.get(cur, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                    cur = str(httpx.URL(cur).join(r.headers["location"]))
                    continue
                r.raise_for_status()
                data = r.content
                break
            if data is None:
                raise ValueError("too many redirects")
        return service.add_torrent_file(data)
    raise ValueError("nothing to add")


# --------------------------------------------------------------------------- the poll

def _max_per_poll() -> int:
    try:
        return max(1, min(50, int(settings_store.get("torrent_rss_max_per_poll") or _DEFAULT_MAX_PER_POLL)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_PER_POLL


async def poll_feed(feed: dict, *, dry_run: bool = False) -> dict:
    """Poll ONE feed. Returns {items, matched, added, skipped, error, primed}.

    `dry_run` fetches and filters but adds nothing and records nothing — that is the "Check now"
    preview, and it is what makes a filter writable: you can see what a feed would take before it
    takes it.
    """
    fid = feed.get("id", "")
    out = {"id": fid, "items": 0, "matched": 0, "added": 0, "skipped": 0,
           "error": "", "primed": False, "titles": []}
    try:
        items = await fetch_feed_items(feed.get("url", ""))
    except Exception as e:
        out["error"] = str(e)
        if not dry_run:
            _record(fid, [], error=str(e))
        return out

    out["items"] = len(items)
    matched = [it for it in items
               if matches(it["title"], feed.get("include", ""), feed.get("exclude", ""))]
    out["matched"] = len(matched)
    out["titles"] = [it["title"] for it in matched[:25]]

    seen = set(_read_store().get("seen", {}).get(fid, []))
    fresh = [it for it in matched if it["id"] not in seen]

    if dry_run:
        out["added"] = 0
        out["skipped"] = len(matched) - len(fresh)
        return out

    # THE FIRST POLL OF A NEW FEED ADDS NOTHING — it learns the backlog. Subscribing must not
    # start 200 downloads; see the module docstring.
    if not feed.get("primed"):
        _record(fid, [it["id"] for it in items], error="", primed=True)
        out["primed"] = True
        out["skipped"] = len(fresh)
        return out

    cap = _max_per_poll()
    took, added_ids, last_fail = 0, [], ""
    for it in fresh:
        if took >= cap:
            break
        try:
            await add_one(it.get("magnet", ""), it.get("torrent_url", ""))
            added_ids.append(it["id"])
            took += 1
            logger.info("[trss] added from feed %s: %s", fid, it["title"][:90])
        except Exception as e:
            last_fail = str(e)
            logger.warning("[trss] could not add %r from feed %s: %s", it["title"][:60], fid, e)
    out["added"] = took
    out["skipped"] = len(fresh) - took
    # A FEED THAT MATCHED THINGS AND ADDED NONE OF THEM IS NOT A CLEAN CHECK. The torrent client
    # can be off, unconfigured or unable to reach its proxy, and every one of those fails here and
    # nowhere else — recorded as success it reads exactly like a feed whose filter matches nothing,
    # which is the wrong thing to go looking at.
    out["error"] = last_fail if (last_fail and not took) else ""
    _record(fid, added_ids, error=out["error"], added=took)
    return out


def _record(fid: str, new_ids: list, *, error: str = "", primed: Optional[bool] = None,
            added: int = 0) -> None:
    """Mark ids seen and stamp the feed's status, in one locked read-modify-write."""
    def _mut(data):
        seen = data.setdefault("seen", {})
        cur = list(seen.get(fid, []))
        for i in new_ids:
            if i not in cur:
                cur.append(i)
        seen[fid] = cur[-_MAX_SEEN_PER_FEED:]
        for f in data.get("feeds", []):
            if f.get("id") == fid:
                f["last_check"] = int(time.time())
                f["last_error"] = error or ""
                f["last_added"] = int(added)
                f["added_total"] = int(f.get("added_total", 0)) + int(added)
                if primed is not None:
                    f["primed"] = bool(primed)
        return data

    try:
        _with_store(_mut)
    except Exception as e:
        logger.error("[trss] could not record feed state: %s", e)


async def poll_all() -> dict:
    feeds = [f for f in _read_store().get("feeds", []) if f.get("enabled", True)]
    total = {"feeds": len(feeds), "added": 0}
    for f in feeds:
        r = await poll_feed(f)
        total["added"] += r.get("added", 0)
    return total


# --------------------------------------------------------------------------- scheduler

_scheduler = None


def _interval_seconds() -> int:
    try:
        m = int(settings_store.get("torrent_rss_interval_minutes") or _DEFAULT_INTERVAL_MIN)
    except (TypeError, ValueError):
        m = _DEFAULT_INTERVAL_MIN
    return max(5, min(24 * 60, m)) * 60


async def _tick():
    # Read the flag EVERY tick, from the settings store the worker hydrated from the relay — not a
    # build-time default, and not a value captured at start. Turning the feature on in Admin then
    # takes effect at the next tick instead of at the next restart.
    if not settings_store.get_bool("torrent_rss_enabled"):
        return
    if not settings_store.get_bool("bt_enabled") and not settings_store.get("bt_server_url"):
        return
    try:
        r = await poll_all()
        if r["added"]:
            logger.info("[trss] polled %d feed(s), added %d torrent(s)", r["feeds"], r["added"])
    except Exception as e:
        logger.error("[trss] poll failed: %s", e, exc_info=True)


def start_torrent_rss_scheduler():
    """Idempotent. The JOB is always scheduled; the tick itself is the gate, so flipping
    `torrent_rss_enabled` on in Admin does not need a worker restart."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    secs = _interval_seconds()
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_tick, IntervalTrigger(seconds=secs), id="torrent-rss",
                       name="Torrent RSS feeds", replace_existing=True,
                       coalesce=True, max_instances=1, misfire_grace_time=secs)
    _scheduler.start()
    logger.info("[trss] scheduler started (poll every %ds)", secs)


def stop_torrent_rss_scheduler():
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown()
        except Exception:
            pass
        _scheduler = None
        logger.info("[trss] scheduler stopped")
