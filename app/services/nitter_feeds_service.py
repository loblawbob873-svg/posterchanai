"""Poll each user's configured Nitter RSS feeds and post rendered "post cards" to their
linked Telegram chat.

A background poller (started from app.main on port 3051, mirroring social_notifications_service)
calls poll_once() on an interval. Per-user config lives in UserSetting:
  - "nitter_feeds": newline-separated RSS URLs (same shape as User.news_sources)
  - "nitter_seen":  JSON {rss_url: [recent guids]} cursor state (managed here)

Only the account's own original tweets (not RTs/replies/empty) are posted, rendered as image
cards via command_service._render_post_card_png and sent with TelegramService.send_photo.
Delivery is gated on the user having a linked Telegram chat AND a non-empty feed list.
"""
import asyncio
import base64
import json
import logging
import re
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

import httpx
from lxml import etree, html as lxml_html
from sqlalchemy.orm import Session

from app.models import User, UserSetting
from app.services import settings_store
from app.services.proxy_utils import get_proxy_config
from app.services.telegram_service import TelegramService

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (compatible; posterchanai-nitter/1.0)"}
_MAX_DL = 20_000_000          # cap media/avatar download size to bound memory
_MAX_SEEN = 200               # guids retained per feed
_MAX_NEW_PER_FEED = 5         # don't flood a chat if a feed jumps ahead
_DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"
# Nitter uses these literal placeholder strings as the RSS <title> of a tweet that
# has NO text (just media), instead of an empty title — so they must be treated as
# image-only and skipped, same as an empty title.
_MEDIA_PLACEHOLDER_TITLES = {"image", "video", "gif"}


# --- settings helpers -------------------------------------------------------

def _get_setting(db: Session, key: str, default: str = "") -> str:
    return settings_store.get(key) or default


def _get_user_setting(db: Session, user_id: int, key: str, default: str = "") -> str:
    s = db.query(UserSetting).filter(UserSetting.user_id == user_id, UserSetting.key == key).first()
    return s.value if s and s.value else default


def _set_user_setting(db: Session, user_id: int, key: str, value: str) -> None:
    s = db.query(UserSetting).filter(UserSetting.user_id == user_id, UserSetting.key == key).first()
    if s:
        s.value = value
    else:
        db.add(UserSetting(user_id=user_id, key=key, value=value))


def _build_telegram(db: Session):
    token = _get_setting(db, "telegram_bot_token")
    if not token:
        return None
    tg = TelegramService(token)
    api_base = _get_setting(db, "telegram_api_base")
    if api_base:
        tg.set_api_base(api_base)
    return tg


# --- Nitter RSS parsing (ported from posterchan/nitterListener.py) ----------

_STATUS_ID_RE = re.compile(r"/status/(\d+)")


def _stable_guid(raw: str) -> str:
    """Collapse a feed item's guid/link to an instance- and format-independent dedup key: the
    numeric tweet status id. nitter instances variously emit <guid> as a bare id, as a full
    permalink URL, or omit it (we then fall back to <link>, a URL) — so the SAME tweet can arrive
    as '123' on one poll and 'https://nitter.net/u/status/123#m' on the next, especially when the
    instance/Tor exit flaps. Comparing raw guids then misses, re-sending the tweet every poll (the
    'non-stop duplicates' bug). Normalising both stored and incoming guids to the status id fixes
    it and self-heals legacy mixed-format state. Falls back to the stripped raw string for any
    non-status guid so nothing is lost."""
    s = (raw or "").strip()
    if s.isdigit():
        return s
    m = _STATUS_ID_RE.search(s)
    return m.group(1) if m else s


def _handle_from_rss(rss_url: str) -> str:
    parts = [p for p in (rss_url or "").split("/") if p]
    if parts and parts[-1].lower() == "rss" and len(parts) >= 2:
        return parts[-2]
    return parts[-1] if parts else "feed"


def _should_skip(item) -> bool:
    """Keep only the account's own original, text-bearing tweets: drop retweets
    (native 'RT by @user:' and classic 'RT @handle:'), replies ('R to ') and
    image-only posts (empty title, or Nitter's 'Image'/'Video'/'GIF' placeholder
    title for a text-less media tweet)."""
    title = (item.findtext("title") or "").strip()
    return ((not title) or title.lower() in _MEDIA_PLACEHOLDER_TITLES
            or title.startswith("RT by ") or title.startswith("RT @")
            or title.startswith("R to "))


def _parse_description(desc: str):
    """(text, media_url) from an item's CDATA-HTML <description>. Text is THIS tweet's
    only (the quote-tweet <blockquote> is dropped). For media, prefer this tweet's own
    <img>, then its <video poster> thumbnail (video/GIF tweets), then fall back to the
    quoted tweet's media — otherwise video and quote-tweets render with no embed."""
    if not desc or not desc.strip():
        return "", ""
    try:
        frag = lxml_html.fromstring(desc)
        # Quoted tweet's media, captured before the blockquote is dropped (last-resort).
        quoted = frag.xpath("//blockquote//img/@src") + frag.xpath("//blockquote//video/@poster")
        for bq in frag.xpath("//blockquote"):
            bq.getparent().remove(bq)
        text = frag.text_content().strip()
        own = frag.xpath("//img/@src") + frag.xpath("//video/@poster")
        media = own or quoted
        return text, (media[0].strip() if media else "")
    except Exception:
        return "", ""


def _fmt_pubdate(pubdate: str) -> str:
    try:
        return parsedate_to_datetime(pubdate).strftime("%b %d, %Y")
    except Exception:
        return ""


def _resolve_pic(url: str) -> str:
    """Nitter /pic/ proxy URL → underlying Twitter CDN URL (more reliable than the
    proxy). Returns the input unchanged if it isn't a /pic/ URL."""
    if not url or "/pic/" not in url:
        return url or ""
    tail = unquote(url.split("/pic/", 1)[1])
    if tail.startswith("http"):
        return tail
    # Nitter encodes the full CDN host in the tail for profile pics
    # ("pbs.twimg.com/..."); media tails may be a bare path ("media/...").
    if tail.startswith("pbs.twimg.com/"):
        return "https://" + tail
    return "https://pbs.twimg.com/" + tail


def _looks_like_feed(content: bytes) -> bool:
    """True only if the body is plausibly an RSS/Atom feed. Guards against forwarding GARBAGE: a
    Cloudflare challenge / 403 / error HTML page often comes back as HTTP 200, and the recover=True
    XML parser would happily turn it into junk 'items'. If it doesn't smell like a feed, we skip it
    (and try the next transport) rather than posting noise."""
    if not content:
        return False
    try:
        head = content[:2048].decode("utf-8", "ignore").lstrip().lower()
    except Exception:
        return False
    return head.startswith("<?xml") or "<rss" in head or "<feed" in head or "<channel" in head


async def _fetch_feed(rss: str, clients):
    """Fetch + parse one RSS feed, trying each transport in order (proxy first, then direct). Returns
    (items, avatar_url, channel_name, working_client) for the FIRST transport that returns a valid,
    parseable feed WITH items — else (None, '', '', None). Only validly-parsed feeds are ever
    returned, so a connection hiccup / CF page can't be forwarded as a tweet."""
    for label, client, timeout in clients:
        try:
            resp = await client.get(rss, headers=_UA, timeout=timeout)
        except Exception as e:
            logger.debug("[nitter] %s fetch %s failed: %s", label, rss[:50], e)
            continue
        if resp.status_code != 200:
            logger.debug("[nitter] %s fetch %s -> HTTP %s", label, rss[:50], resp.status_code)
            continue
        if not _looks_like_feed(resp.content):
            logger.debug("[nitter] %s fetch %s -> not a feed (garbage body), trying next", label, rss[:50])
            continue
        items, avatar_url, channel_name = _parse_feed(resp.content)
        if items:
            return items, avatar_url, channel_name, client
        # 200 + real feed but no items = gate page / empty — try the next transport before giving up.
        logger.debug("[nitter] %s fetch %s -> 0 items (gate/empty), trying next", label, rss[:50])
    return None, "", "", None


def _parse_feed(content: bytes):
    """Parse RSS bytes → (items, avatar_url, channel_name). items newest-first."""
    # Some instances (nitter.net) return a 200 valid-RSS body whose only item is a gate
    # message ("RSS reader not yet whitelisted! Plain request with just ID will be ignored!").
    # Drop the whole feed so it's never forwarded, rather than posting the error as a tweet.
    try:
        # Phrases distinctive to the gate page only — a normal tweet won't contain them, so
        # this can't false-positive and silently stall a real feed.
        if any(s in content.decode("utf-8", "ignore").lower()
               for s in ("not yet whitelist", "plain request with just id")):
            return [], "", ""
    except Exception:
        pass
    root = etree.fromstring(content, parser=etree.XMLParser(recover=True))
    if root is None:
        return [], "", ""
    avatar_url, channel_name = "", ""
    chan = root.find(".//channel")
    img = chan.find("image") if chan is not None else None
    if img is not None:
        avatar_url = (img.findtext("url") or "").strip()
        ctitle = (img.findtext("title") or "").strip()
        channel_name = ctitle.split(" / ", 1)[0].strip() if " / " in ctitle else ctitle
    items = []
    for item in root.findall(".//item"):
        guid = (item.findtext("guid") or item.findtext("link") or "").strip()
        if not guid or _should_skip(item):
            continue
        title = (item.findtext("title") or "").strip()
        text, media_url = _parse_description(item.findtext("description") or "")
        items.append({
            # Normalise to the stable status id so a guid-format/instance flip between polls can't
            # make an already-seen tweet look new (the duplicate-flood bug). See _stable_guid.
            "guid": _stable_guid(guid),
            "title": title,
            "link": (item.findtext("link") or "").strip(),
            "text": text or title,
            "media_url": media_url,
            "timestamp": _fmt_pubdate(item.findtext("pubDate") or ""),
        })
    return items, avatar_url, channel_name


# --- fetch + render ---------------------------------------------------------

async def _download(client: httpx.AsyncClient, url: str):
    """Fetch an image URL → (bytes, content_type) or (None, None), size-capped."""
    if not url:
        return None, None
    try:
        async with client.stream("GET", url, headers=_UA, timeout=30) as r:
            if r.status_code != 200:
                return None, None
            ct = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > _MAX_DL:
                    logger.warning(f"[nitter] media exceeds {_MAX_DL}B, skipping {url[:60]}")
                    return None, None
            return (bytes(buf), ct) if buf else (None, None)
    except Exception as e:
        logger.warning(f"[nitter] download failed for {url[:60]}: {e}")
        return None, None


def _caption(handle: str, item: dict) -> str:
    link = item.get("link", "")
    cap = f"🐦 @{handle}"
    if link:
        cap += f"\n\n{link}"
    return cap


async def _render_card(client: httpx.AsyncClient, item: dict, handle: str,
                       display_name: str, avatar_url: str):
    """Render one item to PNG bytes via the shared headless-browser card renderer,
    or None on failure. Media/avatar are pre-fetched here (server does no extra fetch)."""
    from app.services.command_service import _render_post_card_png

    media_bytes, media_ct = await _download(client, item.get("media_url"))
    avatar_bytes, avatar_ct = await _download(client, _resolve_pic(avatar_url))
    media_uri = f"data:{media_ct or 'image/jpeg'};base64,{base64.b64encode(media_bytes).decode()}" if media_bytes else ""
    avatar_uri = f"data:{avatar_ct or 'image/jpeg'};base64,{base64.b64encode(avatar_bytes).decode()}" if avatar_bytes else ""
    try:
        return await asyncio.to_thread(
            _render_post_card_png,
            display_name or handle, handle, item.get("text") or item.get("title") or "",
            item.get("timestamp") or "", media_uri, avatar_uri,
        )
    except Exception as e:
        logger.warning(f"[nitter] card render failed: {e}")
        return None


# --- per-user poll ----------------------------------------------------------

async def _poll_user(db: Session, tg: TelegramService, user: User, clients) -> None:
    raw = _get_user_setting(db, user.id, "nitter_feeds")
    feeds = [ln.strip() for ln in raw.splitlines() if ln.strip()] if raw else []
    if not feeds:
        return
    chat_id = str(user.telegram_chat_id)
    try:
        seen = json.loads(_get_user_setting(db, user.id, "nitter_seen", "{}") or "{}")
        if not isinstance(seen, dict):
            seen = {}
    except (json.JSONDecodeError, ValueError):
        seen = {}

    changed = False
    for rss in feeds:
        # Proxy-first with direct fallback; only a validly-parsed feed with items comes back.
        items, avatar_url, channel_name, client = await _fetch_feed(rss, clients)
        if not items:
            continue
        handle = _handle_from_rss(rss)

        if rss not in seen:
            # First poll for this feed: establish the baseline without forwarding backlog.
            seen[rss] = [it["guid"] for it in items][:_MAX_SEEN]
            changed = True
            continue

        # Normalise the stored cursor too, so legacy entries saved in the old full-URL form still
        # match incoming (now status-id) guids — otherwise the first poll after this fix would
        # re-send everything once.
        prev_seen = [_stable_guid(g) for g in seen[rss]]
        known = set(prev_seen)
        new_items = [it for it in items if it["guid"] not in known]
        # Oldest-first so chat order is chronological; cap to avoid flooding.
        unrendered = set()
        for it in reversed(new_items[:_MAX_NEW_PER_FEED]):
            png = await _render_card(client, it, handle, channel_name, avatar_url)
            if not png:
                # Transient render failure (avatar/media fetch, browser hiccup): DON'T mark it seen,
                # so it's retried next poll instead of being silently dropped forever.
                unrendered.add(it["guid"])
                continue
            try:
                await tg.send_photo(chat_id, base64.b64encode(png).decode(),
                                    caption=_caption(handle, it), parse_mode="")
            except Exception as e:
                logger.warning(f"[nitter] telegram send failed for user {user.id}: {e}")
        # Record current guids as seen (whether or not each sent — a send failure is NOT retried to
        # avoid duplicate posts), EXCEPT items that failed to render so those retry. Newest-first,
        # capped.
        cur_guids = [it["guid"] for it in items if it["guid"] not in unrendered]
        cur_set = set(cur_guids)
        seen[rss] = (cur_guids + [g for g in prev_seen if g not in cur_set])[:_MAX_SEEN]
        changed = True

    if changed:
        _set_user_setting(db, user.id, "nitter_seen", json.dumps(seen))
        db.commit()


async def poll_once(db: Session) -> None:
    """Poll all users with a linked Telegram chat once. Global admin kill-switch
    'nitter_feeds_enabled' is ON unless explicitly set to 'false'."""
    if _get_setting(db, "nitter_feeds_enabled", "true").lower() == "false":
        return
    tg = _build_telegram(db)
    if not tg:
        return
    users = db.query(User).filter(User.telegram_chat_id.isnot(None)).all()
    if not users:
        return
    # Proxy-FIRST, then DIRECT fallback: try the built-in HTTP proxy first so nitter traffic rides it
    # when it works, then fall straight to a direct connection on any failure/garbage. The proxy try
    # uses a SHORT timeout so a Tor-exit throttle/CF-challenge fails fast and we fall back quickly
    # (Cloudflare often blocks Tor exits — _fetch_feed validates the body so a 200 challenge page is
    # never forwarded). trust_env=False so httpx ignores inherited HTTP(S)_PROXY env and only uses the
    # transport we set explicitly.
    proxy_config = get_proxy_config()
    direct_client = httpx.AsyncClient(follow_redirects=True, proxy=None, trust_env=False)
    proxy_client = (httpx.AsyncClient(follow_redirects=True, proxy=proxy_config, trust_env=False)
                    if proxy_config else None)
    # Each entry: (label, client, per-attempt timeout). Proxy first (fail-fast 12s), then direct (30s).
    clients = ([("proxy", proxy_client, 12)] if proxy_client else []) + [("direct", direct_client, 30)]
    logger.debug("[nitter] transports: %s", ", ".join(c[0] for c in clients))
    try:
        for user in users:
            try:
                await _poll_user(db, tg, user, clients)
            except Exception as e:
                logger.warning(f"[nitter] poll failed for user {user.id}: {e}")
                db.rollback()
    finally:
        await direct_client.aclose()
        if proxy_client is not None:
            await proxy_client.aclose()


# --- scheduler --------------------------------------------------------------

_scheduler = None


def start_nitter_feeds_scheduler() -> None:
    """Start the interval poller (idempotent). Call from a running event loop
    (FastAPI startup), like start_social_notifications_scheduler."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        try:
            secs = int(_get_setting(db, "nitter_feeds_poll_seconds", "300") or "300")
        except ValueError:
            secs = 300
    finally:
        db.close()
    secs = max(60, secs)

    async def _job():
        from app.database import SessionLocal as _SL
        _db = _SL()
        try:
            await poll_once(_db)
        except Exception as e:
            logger.warning(f"[nitter] poll job error: {e}")
        finally:
            _db.close()

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=secs, id="nitter_feeds_poll",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info(f"[nitter] feed poller started (every {secs}s)")


def stop_nitter_feeds_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
