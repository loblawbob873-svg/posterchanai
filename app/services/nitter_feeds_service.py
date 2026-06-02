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
from email.utils import parsedate_to_datetime
from urllib.parse import unquote

import httpx
from lxml import etree, html as lxml_html
from sqlalchemy.orm import Session

from app.models import User, Setting, UserSetting
from app.services.proxy_utils import get_proxy_config
from app.services.telegram_service import TelegramService

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (compatible; posterchanai-nitter/1.0)"}
_MAX_DL = 20_000_000          # cap media/avatar download size to bound memory
_MAX_SEEN = 200               # guids retained per feed
_MAX_NEW_PER_FEED = 5         # don't flood a chat if a feed jumps ahead
_DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"


# --- settings helpers -------------------------------------------------------

def _get_setting(db: Session, key: str, default: str = "") -> str:
    s = db.query(Setting).filter(Setting.key == key).first()
    return s.value if s and s.value else default


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

def _handle_from_rss(rss_url: str) -> str:
    parts = [p for p in (rss_url or "").split("/") if p]
    if parts and parts[-1].lower() == "rss" and len(parts) >= 2:
        return parts[-2]
    return parts[-1] if parts else "feed"


def _should_skip(item) -> bool:
    """Keep only the account's own original, text-bearing tweets: drop retweets
    ('RT by '), replies ('R to ') and image-only posts (empty title)."""
    title = (item.findtext("title") or "").strip()
    return (not title) or title.startswith("RT by ") or title.startswith("R to ")


def _parse_description(desc: str):
    """(text, media_url) from an item's CDATA-HTML <description>. Drops any quote-tweet
    <blockquote> first so text + chosen image come from THIS tweet."""
    if not desc or not desc.strip():
        return "", ""
    try:
        frag = lxml_html.fromstring(desc)
        for bq in frag.xpath("//blockquote"):
            bq.getparent().remove(bq)
        media = frag.xpath("//img/@src")
        return frag.text_content().strip(), (media[0].strip() if media else "")
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
    return tail if tail.startswith("http") else "https://pbs.twimg.com/" + tail


def _parse_feed(content: bytes):
    """Parse RSS bytes → (items, avatar_url, channel_name). items newest-first."""
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
            "guid": guid,
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

async def _poll_user(db: Session, tg: TelegramService, user: User, client: httpx.AsyncClient) -> None:
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
        try:
            resp = await client.get(rss, headers=_UA, timeout=30)
            if resp.status_code != 200:
                continue
            items, avatar_url, channel_name = _parse_feed(resp.content)
        except Exception as e:
            logger.warning(f"[nitter] feed fetch/parse failed ({rss[:60]}) for user {user.id}: {e}")
            continue
        if not items:
            continue
        handle = _handle_from_rss(rss)

        if rss not in seen:
            # First poll for this feed: establish the baseline without forwarding backlog.
            seen[rss] = [it["guid"] for it in items][:_MAX_SEEN]
            changed = True
            continue

        known = set(seen[rss])
        new_items = [it for it in items if it["guid"] not in known]
        # Oldest-first so chat order is chronological; cap to avoid flooding.
        for it in reversed(new_items[:_MAX_NEW_PER_FEED]):
            png = await _render_card(client, it, handle, channel_name, avatar_url)
            if not png:
                continue
            try:
                await tg.send_photo(chat_id, base64.b64encode(png).decode(),
                                    caption=_caption(handle, it), parse_mode="")
            except Exception as e:
                logger.warning(f"[nitter] telegram send failed for user {user.id}: {e}")
        # Record all current guids as seen (whether or not each sent), newest-first, capped.
        cur_guids = [it["guid"] for it in items]
        cur_set = set(cur_guids)
        seen[rss] = (cur_guids + [g for g in seen[rss] if g not in cur_set])[:_MAX_SEEN]
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
    # Route all Nitter requests (RSS fetches + media/avatar downloads) through the
    # built-in proxy, matching 4chan/searx. The proxy is MANDATORY — never make
    # direct Nitter requests. If it's not configured, skip the poll entirely.
    proxy_config = get_proxy_config()
    if not proxy_config:
        logger.warning("[nitter] no proxy configured; skipping poll (direct requests are not allowed)")
        return
    logger.debug("[nitter] requests via proxy: %s", proxy_config)
    async with httpx.AsyncClient(follow_redirects=True, proxy=proxy_config) as client:
        for user in users:
            try:
                await _poll_user(db, tg, user, client)
            except Exception as e:
                logger.warning(f"[nitter] poll failed for user {user.id}: {e}")
                db.rollback()


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
