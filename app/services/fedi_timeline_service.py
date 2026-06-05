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
import asyncio
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
_REPLY_WINDOW_HOURS = 12      # how far back to keep re-checking roots for new federated replies
_REPLY_POLL_INTERVAL = 120    # min seconds between reply re-checks (decoupled from the post poll
                              # so a busy feed's growing root set doesn't hammer the source instance)
_REPLY_MAX_ROOTS = 150        # cap roots re-checked per cycle (newest first)
_REPLY_POLL_BUDGET = 30       # max wall-clock seconds spent re-checking roots per cycle, so the
                              # 150-root catch-up can't drag a single poll out to minutes (it runs
                              # every cycle anyway — deferred roots are picked up next time)
_MAX_ANCESTORS = 12           # cap ancestors backfilled to anchor an orphan reply's conversation
_POLL_TIMEOUT = 90            # hard cap on one poll_once. APScheduler runs us with max_instances=1,
                              # so a single wedged poll would otherwise freeze the whole bridge
                              # indefinitely; cancelling it frees the slot and the next cycle retries.
                              # Kept low so a wedge costs a ~90s gap, not minutes. Normal polls finish
                              # in seconds; a cancelled catch-up just resumes (TimelinePost dedup).
_DOWNLOAD_TIMEOUT = 25       # hard total wall-clock cap on a single remote media/avatar download
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
            "content_emojis": _emoji_url_map(rn.get("emojis")),
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
        "content_emojis": _emoji_url_map(n.get("emojis")),   # custom emoji used in the note text
        "url": n.get("url"),                   # human URL to the post (remote notes only)
        "in_reply_to_id": n.get("replyId"),    # parent note id (for proper thread reply chains)
        "replies_count": n.get("repliesCount") or 0,
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
            "content_emojis": _emoji_url_map(sub.get("emojis")),
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
        "content_emojis": _emoji_url_map(s.get("emojis")),   # custom emoji used in the content
        "url": s.get("url") or s.get("uri"),   # human URL to the post/thread
        "in_reply_to_id": s.get("in_reply_to_id"),  # parent status id (for thread reply chains)
        "replies_count": s.get("replies_count") or 0,
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

# Mention/profile anchors in post HTML. We strip these to plain text: a Matrix client renders a
# URL preview for a fediverse profile link (`/users/x` or `/@x`), which shows the user's whole
# profile card + bio below every post — unwanted bloat. Removing the <a> (keeping the @name text)
# leaves the mention readable without a previewable link.
_MENTION_ANCHOR_RE = re.compile(r'<a\b[^>]*class="[^"]*\bmention\b[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
_PROFILE_ANCHOR_RE = re.compile(r'<a\b[^>]*href="https?://[^"]*?/(?:users/|@)[^"]*"[^>]*>(.*?)</a>', re.S | re.I)


def _strip_profile_links(html_str: str) -> str:
    """Replace mention/profile <a> links with their visible text so clients don't render a
    profile preview card (with bio) below the post."""
    html_str = _MENTION_ANCHOR_RE.sub(r"\1", html_str or "")
    html_str = _PROFILE_ANCHOR_RE.sub(r"\1", html_str)
    return html_str


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


def _body_html(avatar_mxc: str | None, post: dict) -> str:
    a = post["author"]
    name = _apply_emojis(html.escape(a.get("display") or a.get("acct") or ""), a.get("emoji_mxc"))
    avatar = f'<img src="{html.escape(avatar_mxc)}" width="20" height="20" /> ' if avatar_mxc else ""
    # Minimal header: avatar + name only. No @handle, no profile link, no per-post link — those
    # were bloat on every post.
    header = f"{avatar}<strong>{name}</strong>"
    segments = [header]
    if post["text"]:
        # Pleroma content is HTML (strip profile links so no preview card); Misskey is plain text.
        body = _strip_profile_links(post["html"]) if post.get("html") else render_matrix_html(post["text"])
        segments.append(_apply_emojis(body, post.get("content_emoji_mxc")))   # custom emoji → <img>
    q = post.get("quote")
    if q:
        qname = _apply_emojis(html.escape(q.get('display') or q.get('acct') or ''), q.get('emoji_mxc'))
        qhead = f"<strong>{qname}</strong> (@{html.escape(q.get('acct') or '')})"
        if q.get("html"):
            qbody = _strip_profile_links(q["html"])
        elif (q.get("text") or "").strip():
            qbody = render_matrix_html(q["text"])
        else:
            qbody = ""
        qbody = _apply_emojis(qbody, q.get("content_emoji_mxc"))
        block = f"<blockquote>{html.escape(_quote_label(post))} {qhead}" + (f"<br>{qbody}" if qbody else "") + "</blockquote>"
        segments.append(block)
    return "<br><br>".join(segments)


# --- media / avatars --------------------------------------------------------

async def _download(url: str) -> tuple[bytes | None, str]:
    """Download bytes (with a size cap) from an arbitrary remote host. Returns (data, mime)
    or (None, '').

    httpx's read timeout only bounds the gap *between* bytes, so a remote host that trickles
    data a byte at a time can stall the request indefinitely and wedge the whole poll. The
    asyncio.wait_for caps total wall-clock time regardless of how the bytes arrive."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await asyncio.wait_for(client.get(url, headers=_UA), timeout=_DOWNLOAD_TIMEOUT)
            if resp.status_code != 200 or len(resp.content) > _MAX_DL:
                return None, ""
            return resp.content, (resp.headers.get("content-type", "").split(";")[0].strip())
    except Exception as e:
        logger.warning(f"[fedi-timeline] download failed/timeout for {url}: {e}")
        return None, ""


def _img_dims(data: bytes, max_w: int = 480) -> tuple[int | None, int | None]:
    """Pixel dimensions for an inline <img> (scaled down to max_w so it renders at a sane size)."""
    try:
        from PIL import Image as _PILImage
        from io import BytesIO as _BytesIO
        with _PILImage.open(_BytesIO(data)) as im:
            w, h = im.width, im.height
        if w and w > max_w:
            h = max(1, int(h * max_w / w))
            w = max_w
        return w, h
    except Exception:
        return None, None


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
    # Resolve custom-emoji shortcodes in the display names to inline Matrix images.
    post["author"]["emoji_mxc"] = await _resolve_name_emojis(
        db, hs, bot_token, post["author"].get("display") or "", post["author"].get("emojis") or {})
    post["content_emoji_mxc"] = await _resolve_name_emojis(
        db, hs, bot_token, post.get("text") or "", post.get("content_emojis") or {})
    if post.get("quote"):
        post["quote"]["emoji_mxc"] = await _resolve_name_emojis(
            db, hs, bot_token, post["quote"].get("display") or "", post["quote"].get("emojis") or {})
        post["quote"]["content_emoji_mxc"] = await _resolve_name_emojis(
            db, hs, bot_token, post["quote"].get("text") or "", post["quote"].get("content_emojis") or {})
    # For a thread reply, point m.in_reply_to at the actual parent message (if we delivered it)
    # so Element renders the real reply chain instead of every reply hanging off the root.
    parent_event = None
    if thread_root_event_id and post.get("in_reply_to_id"):
        # Prefer the parent's TEXT event (delivered first → lowest id) over its media events,
        # so the reply quotes the parent message, not the parent's image.
        prow = db.query(TimelinePost).filter(
            TimelinePost.room_id == room_id, TimelinePost.note_id == post["in_reply_to_id"]
        ).order_by(TimelinePost.id.asc()).first()
        if prow:
            parent_event = prow.event_id
    body_text = _body_text(post)
    body_html = _body_html(avatar_mxc, post)

    def _record(ev_id: str, is_root_event: bool):
        db.add(TimelinePost(
            room_id=room_id, event_id=ev_id,
            # The post event keeps the caller's thread relation; extra media events hang off it.
            thread_root_event_id=thread_root_event_id if is_root_event else (thread_root_event_id or event_id),
            platform=platform, instance_url=instance_url, note_id=post["id"],
            note_uri=uri, author_acct=post["author"].get("acct"),
            body=body_text if is_root_event else None,
        ))
        db.commit()

    # Inline IMAGES into the post's HTML so it's ONE message with the name on top (Matrix HTML
    # allows <img src=mxc> but not <video>). Videos can't be inlined → sent as separate m.video
    # messages after. Download once, sort into inline images vs videos.
    inline_imgs = []
    videos = []
    for m in post["media"]:
        data, mime = await _download(m["url"])
        if not data:
            continue
        sniff, _ = matrix_service._detect_mime(data)
        eff_mime = sniff if sniff.startswith("video/") else (mime or m.get("mime", "") or sniff)
        if eff_mime.startswith("video/"):
            videos.append((data, eff_mime))
            continue
        try:
            mxc = await matrix_service.upload_media_bytes(hs, bot_token, data, eff_mime or "image/jpeg", "image")
        except Exception as e:
            logger.warning(f"[fedi-timeline] inline image upload failed: {e}")
            continue
        w, h = _img_dims(data)
        dim = f' width="{w}" height="{h}"' if w else ""
        inline_imgs.append(f'<img src="{html.escape(mxc)}"{dim} />')

    full_html = body_html + ("<br>" + "<br>".join(inline_imgs) if inline_imgs else "")
    event_id = await matrix_service.send_event(
        hs, bot_token, room_id, body_text, html=full_html,
        thread_root_event_id=thread_root_event_id, reply_to_event_id=parent_event,
    )
    _record(event_id, is_root_event=True)
    # Videos follow as their own m.video events (recorded so interacting resolves to the post).
    for data, mime in videos:
        try:
            mid = await matrix_service.send_image(hs, bot_token, room_id, data, mime=mime,
                                                  thread_root_event_id=thread_root_event_id)
            if mid:
                _record(mid, is_root_event=False)
        except Exception as e:
            logger.warning(f"[fedi-timeline] video send failed: {e}")
    return event_id


async def _fetch_descendants(platform: str, instance_url: str, token: str, note_id: str) -> list[dict]:
    if platform == "misskey":
        return await misskey_service.fetch_children(instance_url, token, note_id)
    ctx = await pleroma_service.fetch_context(instance_url, token, note_id)
    return ctx.get("descendants") or []


async def _deliver_descendants(db: Session, hs: str, bot_token: str, room_id: str, platform: str,
                               instance_url: str, token: str, note_id: str, root_event_id: str) -> None:
    """Fetch a post's fediverse replies and deliver any not-yet-mirrored ones as thread children
    of root_event_id, so the Matrix thread shows the conversation."""
    try:
        children = await _fetch_descendants(platform, instance_url, token, note_id)
    except Exception as e:
        logger.warning(f"[fedi-timeline] descendants fetch failed for {note_id}: {e}")
        return
    for child in sorted((_norm(platform, c) for c in children), key=lambda p: p.get("created_at") or ""):
        uri = _canonical_uri(platform, instance_url, child)
        if not child["id"] or _seen(db, room_id, child["id"], uri):
            continue
        try:
            await _deliver(db, hs, bot_token, room_id, platform, instance_url, child,
                           thread_root_event_id=root_event_id)
        except Exception as e:
            logger.warning(f"[fedi-timeline] reply deliver failed: {e}")


def _find_parent(db: Session, room_id: str, note_id: str):
    return db.query(TimelinePost).filter(
        TimelinePost.room_id == room_id, TimelinePost.note_id == note_id
    ).order_by(TimelinePost.id.desc()).first()


async def _backfill_ancestors(db: Session, hs: str, bot_token: str, room_id: str, platform: str,
                              instance_url: str, token: str, post: dict) -> None:
    """Deliver a reply's ancestors (the chain toward the conversation root) so it threads under
    the true root instead of fragmenting when the original post isn't in the feed. Ancestors are
    ordered root-first; each one threads under the previous, so the chain becomes one thread."""
    try:
        if platform == "pleroma":
            ctx = await pleroma_service.fetch_context(instance_url, token, post["id"])
            ancestors = ctx.get("ancestors") or []
        else:
            ancestors = list(reversed(await misskey_service.fetch_conversation(instance_url, token, post["id"])))
    except Exception as e:
        logger.warning(f"[fedi-timeline] ancestor backfill failed for {post.get('id')}: {e}")
        return
    for raw in ancestors[-_MAX_ANCESTORS:]:        # closest N (always includes the immediate parent)
        anc = _norm(platform, raw)
        uri = _canonical_uri(platform, instance_url, anc)
        if not anc.get("id") or _seen(db, room_id, anc["id"], uri):
            continue
        a_root = None
        if anc.get("in_reply_to_id"):
            p = _find_parent(db, room_id, anc["in_reply_to_id"])
            if p:
                a_root = p.thread_root_event_id or p.event_id
        try:
            await _deliver(db, hs, bot_token, room_id, platform, instance_url, anc, thread_root_event_id=a_root)
        except Exception as e:
            logger.warning(f"[fedi-timeline] ancestor deliver failed: {e}")


async def _poll_replies(db: Session, hs: str, bot_token: str, room_id: str, platform: str,
                        token: str) -> None:
    """Re-check recent roots for new federated replies (they arrive after the root is posted)."""
    cutoff = datetime.utcnow() - timedelta(hours=_REPLY_WINDOW_HOURS)
    roots = db.query(TimelinePost).filter(
        TimelinePost.room_id == room_id,
        TimelinePost.thread_root_event_id.is_(None),
        TimelinePost.created_at >= cutoff,
    ).order_by(TimelinePost.id.desc()).limit(_REPLY_MAX_ROOTS).all()
    deadline = time.monotonic() + _REPLY_POLL_BUDGET
    for root in roots:
        if time.monotonic() > deadline:
            logger.info("[fedi-timeline] reply re-check budget hit; deferring remaining roots")
            break
        # One slow/failing root must not abort the whole re-check (or, via poll_once, the cursor advance).
        try:
            await _deliver_descendants(db, hs, bot_token, room_id, platform, root.instance_url,
                                       token, root.note_id, root.event_id)
        except Exception as e:
            logger.warning(f"[fedi-timeline] reply re-check failed for root {root.note_id}: {e}")


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
                    # The timeline delivers replies as flat standalone items. If this post is a
                    # reply to one already in the room, thread it under that conversation instead
                    # of posting a new root — so a whole conversation shows as one Element thread.
                    thread_root = None
                    if include_replies and post.get("in_reply_to_id"):
                        parent = _find_parent(db, room_id, post["in_reply_to_id"])
                        if not parent:
                            # Orphan reply (its parent/root isn't in the room yet) → backfill the
                            # ancestor chain so the whole conversation threads under one real root.
                            await _backfill_ancestors(db, hs, bot_token, room_id, platform, instance_url, token, post)
                            parent = _find_parent(db, room_id, post["in_reply_to_id"])
                        if parent:
                            thread_root = parent.thread_root_event_id or parent.event_id
                    event_id = await _deliver(db, hs, bot_token, room_id, platform, instance_url, post,
                                              thread_root_event_id=thread_root)
                    # For a brand-new root that already has replies on the fediverse, backfill them
                    # into its thread now so "Reply in thread" shows the conversation immediately.
                    if include_replies and event_id and thread_root is None and post.get("replies_count", 0) > 0:
                        await _deliver_descendants(db, hs, bot_token, room_id, platform, instance_url,
                                                   token, post["id"], event_id)
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
            await asyncio.wait_for(poll_once(_db), timeout=_POLL_TIMEOUT)
        except asyncio.TimeoutError:
            # A wedged poll would pin the max_instances=1 slot forever and freeze the bridge.
            # Cancelling is safe: the cursor only advances after the delivery loop, and TimelinePost
            # dedup (_seen) means a re-fetched batch won't double-post.
            logger.warning(f"[fedi-timeline] poll exceeded {_POLL_TIMEOUT}s and was cancelled; retrying next cycle")
            _db.rollback()
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
