"""Bridge one Misskey/Pleroma timeline into a single Matrix room.

A background poller (started from app.main on port 3051, mirroring social_notifications_service)
mirrors a shared Home/Global/Local timeline from one source instance into one admin-configured
Matrix room. Each post is sent as a thread root; its media and any federated replies are posted
as thread children. Members ❤ favourite / 🔁 boost / reply from Element — handled by the
posterchan bot, which calls POST /api/matrix/timeline-action; each action runs under that
member's own linked fediverse account (see app/routers/matrix.py).

Config is global (app/schemas.py:SettingsResponse `fedi_timeline_*`); the cursor lives in a
separate Setting row `fedi_timeline_since`. State (TimelinePost / MatrixAvatarCache) is in the
DB, but the poller itself is per-process — correct only on the single port-3051 instance, like
the social poller.
"""
import html
import logging
import re
import time
from datetime import datetime, timedelta

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Setting, TimelinePost, MatrixAvatarCache
from app.services import misskey_service, pleroma_service, matrix_service
from app.services.matrix_service import render_matrix_html

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (compatible; posterchanai-fedi-timeline/1.0)"}
_MAX_DL = 50_000_000          # cap media/avatar download size to bound memory
_REPLY_WINDOW_HOURS = 6       # how far back to keep re-checking roots for new federated replies
_REPLY_POLL_INTERVAL = 120    # min seconds between reply re-checks (decoupled from the post poll
                              # so a busy feed's growing root set doesn't hammer the source instance)
_REPLY_MAX_ROOTS = 60         # cap roots re-checked per cycle (newest first)
_last_reply_poll = 0.0        # monotonic ts of the last reply re-check (per-process)
_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>|</\s*p\s*>", re.IGNORECASE)


# --- settings helpers -------------------------------------------------------

def _get_setting(db: Session, key: str, default: str = "") -> str:
    s = db.query(Setting).filter(Setting.key == key).first()
    return s.value if s and s.value else default


def _set_setting(db: Session, key: str, value: str) -> None:
    s = db.query(Setting).filter(Setting.key == key).first()
    if s:
        s.value = value
    else:
        db.add(Setting(key=key, value=value))


# --- normalization (raw platform object -> common shape) --------------------

def _strip_html(raw: str) -> str:
    text = _BREAK_RE.sub("\n", raw or "")
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def _norm_misskey(n: dict) -> dict:
    user = n.get("user") or {}
    name = user.get("username", "?")
    host = user.get("host")
    acct = f"{name}@{host}" if host else name
    media = [{"url": f.get("url"), "mime": f.get("type", "")} for f in (n.get("files") or []) if f.get("url")]
    # A renote with text is a quote; a renote without text is a plain boost. Either way capture
    # the quoted/boosted note so it isn't lost when the post is rendered.
    quote = None
    rn = n.get("renote")
    if rn:
        ru = rn.get("user") or {}
        rname = ru.get("username", "?")
        rhost = ru.get("host")
        quote = {
            "acct": f"{rname}@{rhost}" if rhost else rname,
            "display": ru.get("name") or rname,
            "text": rn.get("text") or "",
            "html": None,
            "emojis": _emoji_url_map(ru.get("emojis")),
        }
    return {
        "id": n.get("id"),
        "uri": n.get("uri") or n.get("url"),   # local notes carry neither; canonicalized later
        "author": {
            "acct": acct,
            "display": user.get("name") or name,
            "avatar_url": user.get("avatarUrl"),
            "url": user.get("url"),            # remote users only; local synthesized later
            "emojis": _emoji_url_map(user.get("emojis")),
        },
        "text": n.get("text") or "",
        "html": None,                          # Misskey text is MFM/plain → render to HTML
        "media": media,
        "quote": quote,
        "created_at": n.get("createdAt"),
    }


def _norm_pleroma(s: dict) -> dict:
    acct_obj = s.get("account") or {}
    media = [{"url": m.get("url"), "mime": ""} for m in (s.get("media_attachments") or []) if m.get("url")]
    # `quote` = a quote-post's quoted status; `reblog` (with empty content) = a plain boost.
    quote = None
    sub = s.get("quote") or s.get("reblog")
    if sub:
        sub_acct = sub.get("account") or {}
        quote = {
            "acct": sub_acct.get("acct") or sub_acct.get("username", "?"),
            "display": sub_acct.get("display_name") or sub_acct.get("username", ""),
            "text": _strip_html(sub.get("content", "")),
            "html": sub.get("content"),
            "emojis": _emoji_url_map(sub_acct.get("emojis")),
        }
    return {
        "id": s.get("id"),
        "uri": s.get("uri") or s.get("url"),   # always present for local statuses
        "author": {
            "acct": acct_obj.get("acct") or acct_obj.get("username", "?"),
            "display": acct_obj.get("display_name") or acct_obj.get("username", ""),
            "avatar_url": acct_obj.get("avatar"),
            "url": acct_obj.get("url"),        # profile page on the author's instance
            "emojis": _emoji_url_map(acct_obj.get("emojis")),
        },
        "text": _strip_html(s.get("content", "")),
        "html": s.get("content"),              # Pleroma content is already HTML
        "media": media,
        "quote": quote,
        "created_at": s.get("created_at"),
    }


def _norm(platform: str, raw: dict) -> dict:
    return _norm_misskey(raw) if platform == "misskey" else _norm_pleroma(raw)


def _canonical_uri(platform: str, instance_url: str, post: dict) -> str | None:
    """The cross-instance AP URI used to resolve a post on a member's own instance and to
    dedup federated copies. Misskey local notes have no `uri`, so synthesize the canonical one."""
    if post.get("uri"):
        return post["uri"]
    if platform == "misskey" and post.get("id"):
        return f"{instance_url.rstrip('/')}/notes/{post['id']}"
    return None


# --- rendering --------------------------------------------------------------

# A fedi handle in plain text: @user or @user@host. The lookbehind keeps it from matching
# inside a URL (preceded by /) , an href value (preceded by "), an email/word (preceded by a
# word char), or a doubled @. A preceding ">" (end of a <br>/tag) is allowed so mentions at
# the start of a line still linkify.
_MENTION_RE = re.compile(r'(?<![\w@/"])@([A-Za-z0-9_]+)(?:@([A-Za-z0-9.\-]+))?')


def _profile_url(instance_url: str, post: dict) -> str | None:
    """The author's profile page: their `url` if the platform gave one, else derived from the
    handle (`@user@host` → https://host/@user; local `@user` → on the source instance)."""
    a = post["author"]
    if a.get("url"):
        return a["url"]
    acct = a.get("acct") or ""
    if not acct:
        return None
    if "@" in acct:
        user, _, host = acct.partition("@")
        return f"https://{host}/@{user}"
    return f"{instance_url.rstrip('/')}/@{acct}"


def _host_of(instance_url: str) -> str:
    return instance_url.split("://", 1)[-1].rstrip("/")


def _linkify_mentions(body_html: str, instance_url: str) -> str:
    """Turn @user@host / @user handles in already-rendered HTML into clickable profile links
    (so a mentioned user is clickable too, mirroring Matrix mention pills)."""
    default_host = _host_of(instance_url)

    def repl(m: "re.Match") -> str:
        user, host = m.group(1), m.group(2)
        url = f"https://{host or default_host}/@{user}"
        shown = f"@{user}@{host}" if host else f"@{user}"
        return f'<a href="{url}">{shown}</a>'

    return _MENTION_RE.sub(repl, body_html)


def _quote_label(post: dict) -> str:
    # A quote with no own text is really a boost; otherwise it's a quote-post.
    return "🔁 boosted" if not post.get("text") else "↪ quoting"


# Custom-emoji shortcode in a name/text: :blobcat: or :blobcat@host: (remote Misskey emoji).
_EMOJI_SHORTCODE_RE = re.compile(r':([a-zA-Z0-9_+\-]+(?:@[a-zA-Z0-9.\-]+)?):')


def _emoji_url_map(raw) -> dict:
    """Normalize a platform emoji field (Pleroma list of {shortcode,url}; Misskey dict
    {name: url} or list) to {shortcode: url}."""
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if v}
    if isinstance(raw, list):
        out = {}
        for e in raw:
            sc = e.get("shortcode") or e.get("name")
            url = e.get("url") or e.get("static_url")
            if sc and url:
                out[sc] = url
        return out
    return {}


async def _resolve_name_emojis(db: Session, hs: str, bot_token: str, display: str, url_map: dict) -> dict:
    """For each :shortcode: used in display, upload its image (cached) and return {shortcode: mxc}."""
    out = {}
    if not display or not url_map:
        return out
    for sc in set(_EMOJI_SHORTCODE_RE.findall(display)):
        url = url_map.get(sc)
        if not url:
            continue
        mxc = await _avatar_mxc(db, hs, bot_token, url)   # reuses the URL→mxc upload cache
        if mxc:
            out[sc] = mxc
    return out


def _apply_emojis(escaped_name: str, mxc_map: dict) -> str:
    """Replace :shortcode: tokens in already-escaped text with inline custom-emoji <img>."""
    if not mxc_map:
        return escaped_name

    def repl(m):
        sc = m.group(1)
        mxc = mxc_map.get(sc)
        if not mxc:
            return m.group(0)
        return (f'<img data-mx-emoticon src="{html.escape(mxc)}" '
                f'alt=":{html.escape(sc)}:" title=":{html.escape(sc)}:" height="20" />')

    return _EMOJI_SHORTCODE_RE.sub(repl, escaped_name)


def _body_text(post: dict) -> str:
    a = post["author"]
    parts = [a.get("display") or a.get("acct") or "?"]
    if post["text"]:
        parts.append(post["text"])
    q = post.get("quote")
    if q:
        qhead = q.get("display") or q.get("acct") or "?"
        qtext = (q.get("text") or "").strip()
        parts.append(f"{_quote_label(post)} {qhead}:" + (f"\n{qtext}" if qtext else ""))
    return "\n\n".join(parts)


def _body_html(avatar_mxc: str | None, post: dict, profile_url: str | None, instance_url: str) -> str:
    a = post["author"]
    name = _apply_emojis(html.escape(a.get("display") or a.get("acct") or ""), a.get("emoji_mxc"))
    avatar = f'<img src="{html.escape(avatar_mxc)}" width="20" height="20" /> ' if avatar_mxc else ""
    # Compact header: avatar + name only (the @handle suffix is bloat on every post). The name
    # still links to the author's profile (tap to view/follow/interact).
    label = f"{avatar}<strong>{name}</strong>"
    header = f'<a href="{html.escape(profile_url)}">{label}</a>' if profile_url else label
    segments = [header]
    if post["text"]:
        segments.append(post["html"] if post.get("html")     # Pleroma already renders mentions
                        else _linkify_mentions(render_matrix_html(post["text"]), instance_url))
    q = post.get("quote")
    if q:
        qname = _apply_emojis(html.escape(q.get('display') or q.get('acct') or ''), q.get('emoji_mxc'))
        qhead = f"<strong>{qname}</strong> (@{html.escape(q.get('acct') or '')})"
        if q.get("html"):
            qbody = q["html"]
        elif (q.get("text") or "").strip():
            qbody = _linkify_mentions(render_matrix_html(q["text"]), instance_url)
        else:
            qbody = ""
        block = f"<blockquote>{html.escape(_quote_label(post))} {qhead}" + (f"<br>{qbody}" if qbody else "") + "</blockquote>"
        segments.append(block)
    return "<br><br>".join(segments)


# --- media / avatars --------------------------------------------------------

async def _download(url: str) -> tuple[bytes | None, str]:
    """Download bytes (with a size cap). Returns (data, mime) or (None, '')."""
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=_UA)
            if resp.status_code != 200 or len(resp.content) > _MAX_DL:
                return None, ""
            return resp.content, (resp.headers.get("content-type", "").split(";")[0].strip())
    except Exception as e:
        logger.warning(f"[fedi-timeline] download failed for {url}: {e}")
        return None, ""


async def _avatar_mxc(db: Session, hs: str, bot_token: str, avatar_url: str | None) -> str | None:
    """Upload an author's avatar to Matrix media (cached by source URL)."""
    if not avatar_url:
        return None
    row = db.query(MatrixAvatarCache).filter(MatrixAvatarCache.author_avatar_url == avatar_url).first()
    if row:
        return row.mxc
    data, mime = await _download(avatar_url)
    if not data:
        return None
    try:
        mxc = await matrix_service.upload_media_bytes(hs, bot_token, data, mime or "image/jpeg", "avatar")
    except Exception as e:
        logger.warning(f"[fedi-timeline] avatar upload failed: {e}")
        return None
    db.add(MatrixAvatarCache(author_avatar_url=avatar_url, mxc=mxc))
    db.commit()
    return mxc


# --- dedup / delivery -------------------------------------------------------

def _seen(db: Session, room_id: str, note_id: str | None, note_uri: str | None) -> bool:
    """True if this note was already posted to the room. Matched on the canonical URI
    (cross-instance) with a note_id fallback for same-instance lookups."""
    conds = []
    if note_id:
        conds.append(TimelinePost.note_id == note_id)
    if note_uri:
        conds.append(TimelinePost.note_uri == note_uri)
    if not conds:
        return False
    return db.query(TimelinePost.id).filter(
        TimelinePost.room_id == room_id, or_(*conds)
    ).first() is not None


async def _deliver(db: Session, hs: str, bot_token: str, room_id: str, platform: str,
                   instance_url: str, post: dict, thread_root_event_id: str | None = None) -> str:
    """Post a normalized note (root if thread_root_event_id is None, else a thread child),
    record it, and post its media as thread children. Returns the new root event_id."""
    uri = _canonical_uri(platform, instance_url, post)
    avatar_mxc = await _avatar_mxc(db, hs, bot_token, post["author"].get("avatar_url"))
    profile_url = _profile_url(instance_url, post)
    # Resolve custom-emoji shortcodes in the display names to inline Matrix images.
    post["author"]["emoji_mxc"] = await _resolve_name_emojis(
        db, hs, bot_token, post["author"].get("display") or "", post["author"].get("emojis") or {})
    if post.get("quote"):
        post["quote"]["emoji_mxc"] = await _resolve_name_emojis(
            db, hs, bot_token, post["quote"].get("display") or "", post["quote"].get("emojis") or {})
    event_id = await matrix_service.send_event(
        hs, bot_token, room_id, _body_text(post),
        html=_body_html(avatar_mxc, post, profile_url, instance_url),
        thread_root_event_id=thread_root_event_id,
    )
    db.add(TimelinePost(
        room_id=room_id, event_id=event_id, thread_root_event_id=thread_root_event_id,
        platform=platform, instance_url=instance_url, note_id=post["id"],
        note_uri=uri, author_acct=post["author"].get("acct"), body=_body_text(post),
    ))
    db.commit()
    # Attachments: a root post's own media goes inline (no thread relation) so it shows WITH the
    # post; a reply's media goes into the reply's thread. This keeps threads = actual replies only,
    # so Element renders them cleanly.
    media_root = thread_root_event_id
    for m in post["media"]:
        data, mime = await _download(m["url"])
        if not data:
            continue
        try:
            await matrix_service.send_image(hs, bot_token, room_id, data, mime=mime or m.get("mime", ""),
                                            thread_root_event_id=media_root)
        except Exception as e:
            logger.warning(f"[fedi-timeline] media send failed: {e}")
    return event_id


async def _fetch_descendants(platform: str, instance_url: str, token: str, note_id: str) -> list[dict]:
    if platform == "misskey":
        return await misskey_service.fetch_children(instance_url, token, note_id)
    ctx = await pleroma_service.fetch_context(instance_url, token, note_id)
    return ctx.get("descendants") or []


async def _poll_replies(db: Session, hs: str, bot_token: str, room_id: str, platform: str,
                        token: str) -> None:
    """Re-check recent roots for new federated replies (they arrive after the root is posted)."""
    cutoff = datetime.utcnow() - timedelta(hours=_REPLY_WINDOW_HOURS)
    roots = db.query(TimelinePost).filter(
        TimelinePost.room_id == room_id,
        TimelinePost.thread_root_event_id.is_(None),
        TimelinePost.created_at >= cutoff,
    ).order_by(TimelinePost.id.desc()).limit(_REPLY_MAX_ROOTS).all()
    for root in roots:
        try:
            children = await _fetch_descendants(platform, root.instance_url, token, root.note_id)
        except Exception as e:
            logger.warning(f"[fedi-timeline] descendants fetch failed for {root.note_id}: {e}")
            continue
        for child in sorted((_norm(platform, c) for c in children), key=lambda p: p.get("created_at") or ""):
            uri = _canonical_uri(platform, root.instance_url, child)
            if not child["id"] or _seen(db, room_id, child["id"], uri):
                continue
            try:
                await _deliver(db, hs, bot_token, room_id, platform, root.instance_url, child,
                               thread_root_event_id=root.event_id)
            except Exception as e:
                logger.warning(f"[fedi-timeline] reply deliver failed: {e}")


# --- poll loop --------------------------------------------------------------

async def poll_once(db: Session) -> None:
    if _get_setting(db, "fedi_timeline_enabled", "false").lower() != "true":
        return
    platform = _get_setting(db, "fedi_timeline_platform", "misskey")
    instance_url = _get_setting(db, "fedi_timeline_instance_url")
    token = _get_setting(db, "fedi_timeline_token")
    hs = _get_setting(db, "fedi_timeline_matrix_homeserver").rstrip("/")
    bot_token = _get_setting(db, "fedi_timeline_matrix_bot_token")
    room_id = _get_setting(db, "fedi_timeline_room_id")
    if not (instance_url and token and hs and bot_token and room_id):
        return
    ttype = _get_setting(db, "fedi_timeline_type", "home")
    include_replies = _get_setting(db, "fedi_timeline_include_replies", "true").lower() == "true"
    since = _get_setting(db, "fedi_timeline_since")

    if platform == "misskey":
        raw_posts = await misskey_service.fetch_timeline(instance_url, token, ttype, since_id=since or None)
    else:
        raw_posts = await pleroma_service.fetch_timeline(instance_url, token, ttype, since_id=since or None)

    if raw_posts:
        # Sort by created_at (ISO8601 → lexical == chronological) rather than trusting the
        # API's order: Misskey returns ascending when sinceId is set but descending without it,
        # so neither raw_posts[0] nor reversed() is reliably "newest"/oldest-first.
        posts = sorted((_norm(platform, r) for r in raw_posts), key=lambda p: p.get("created_at") or "")
        newest_id = posts[-1].get("id")
        if not since:
            # First poll: set the cursor without backfilling the existing timeline.
            _set_setting(db, "fedi_timeline_since", newest_id)
            db.commit()
        else:
            for post in posts:                  # oldest-first so room order is chronological
                uri = _canonical_uri(platform, instance_url, post)
                if not post["id"] or _seen(db, room_id, post["id"], uri):
                    continue
                try:
                    await _deliver(db, hs, bot_token, room_id, platform, instance_url, post)
                except Exception as e:
                    logger.warning(f"[fedi-timeline] post deliver failed: {e}")
            _set_setting(db, "fedi_timeline_since", newest_id)
            db.commit()

    # Re-check recent roots for replies that federated in after they were posted. Throttled
    # well below the post-poll cadence so a high-volume feed doesn't hammer the source instance.
    global _last_reply_poll
    now = time.monotonic()
    if include_replies and since and (now - _last_reply_poll) >= _REPLY_POLL_INTERVAL:
        _last_reply_poll = now
        await _poll_replies(db, hs, bot_token, room_id, platform, token)


# --- scheduler --------------------------------------------------------------

_scheduler = None


def start_fedi_timeline_scheduler() -> None:
    """Start the interval poller (idempotent). Must be called from within a running event
    loop (e.g. FastAPI startup), like start_social_notifications_scheduler."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        try:
            secs = int(_get_setting(db, "fedi_timeline_poll_seconds", "90") or "90")
        except ValueError:
            secs = 90
    finally:
        db.close()
    secs = max(15, secs)

    async def _job():
        from app.database import SessionLocal as _SL
        _db = _SL()
        try:
            await poll_once(_db)
        except Exception as e:
            logger.warning(f"[fedi-timeline] poll job error: {e}")
            _db.rollback()
        finally:
            _db.close()

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=secs, id="fedi_timeline_poll", max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info(f"[fedi-timeline] timeline bridge poller started (every {secs}s)")


def stop_fedi_timeline_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"[fedi-timeline] scheduler shutdown error: {e}")
        _scheduler = None
