"""Relay Pleroma / Misskey / Matrix notifications to a user's Telegram chat, and post
replies back to the originating platform when the user replies to a forwarded message.

A background poller (started from app.main on port 3051, mirroring logs_scheduler) calls
poll_once() on an interval. The Telegram webhook handler calls handle_reply() when a user
replies to one of the forwarded notification messages.
"""
import html
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models import User, Setting, SocialReplyMap
from app.services import misskey_service, pleroma_service, matrix_service
from app.services.telegram_service import TelegramService

logger = logging.getLogger(__name__)

_REPLY_MAP_TTL_DAYS = 7
_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>|</\s*p\s*>", re.IGNORECASE)


# --- settings helpers -------------------------------------------------------

def _get_setting(db: Session, key: str, default: str = "") -> str:
    s = db.query(Setting).filter(Setting.key == key).first()
    return s.value if s and s.value else default


def _build_telegram(db: Session) -> Optional[TelegramService]:
    token = _get_setting(db, "telegram_bot_token")
    if not token:
        return None
    tg = TelegramService(token)
    api_base = _get_setting(db, "telegram_api_base")
    if api_base:
        tg.set_api_base(api_base)
    return tg


# --- normalization (raw platform object -> common shape) --------------------

def _strip_html(raw: str) -> str:
    # Pleroma/Mastodon status content is HTML: turn block/line breaks into newlines,
    # drop remaining tags, then unescape entities (&quot;, &amp;, &#39;, …).
    text = _BREAK_RE.sub("\n", raw or "")
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def _norm_misskey(n: dict) -> dict:
    actor = n.get("user") or {}
    note = n.get("note") or {}
    handle = actor.get("username", "?")
    host = actor.get("host")
    actor_str = f"@{handle}@{host}" if host else f"@{handle}"
    return {
        "platform": "misskey",
        "type": n.get("type", "notification"),
        "actor": actor_str,
        "text": note.get("text") or "",
        "reply_target": note.get("id"),
        "room_id": None,
        "event_id": None,
        "visibility": note.get("visibility", "public"),
        "url": None,
    }


def _norm_pleroma(n: dict) -> dict:
    acct = n.get("account") or {}
    status = n.get("status") or {}
    actor_str = "@" + (acct.get("acct") or acct.get("username", "?"))
    return {
        "platform": "pleroma",
        "type": n.get("type", "notification"),
        "actor": actor_str,
        "text": _strip_html(status.get("content", "")) if status else "",
        "reply_target": status.get("id") if status else None,
        "room_id": None,
        "event_id": None,
        "visibility": status.get("visibility", "public") if status else "public",
        "url": status.get("url") if status else None,
    }


def _norm_matrix(ev: dict) -> dict:
    if ev.get("encrypted"):
        # Undecryptable DM: notify only, no reply target (we can't post encrypted).
        return {
            "platform": "matrix",
            "type": "encrypted DM",
            "actor": "a contact",
            "text": "🔒 You received an encrypted message. Open Element to read and reply.",
            "reply_target": None,
            "room_id": None,
            "event_id": None,
            "visibility": None,
            "url": None,
            "encrypted": True,
        }
    return {
        "platform": "matrix",
        "type": "message",
        "actor": ev.get("sender", "?"),
        "text": ev.get("body", ""),
        "reply_target": None,
        "room_id": ev.get("room_id"),
        "event_id": ev.get("event_id"),
        "visibility": None,
        "url": None,
    }


_PLATFORM_ICON = {"misskey": "🍮", "pleroma": "💧", "matrix": "🟩"}


def _format(norm: dict) -> str:
    # Plain text (no Markdown): notification bodies contain arbitrary characters that break
    # Telegram's Markdown parser, causing a failed send + retry on every message.
    icon = _PLATFORM_ICON.get(norm["platform"], "🔔")
    label = norm["platform"].upper()
    lines = [
        f"{icon} 【 {label} · {norm['type']} 】",
        f"👤 {norm['actor']}",
    ]
    if norm.get("text"):
        lines += ["━━━━━━━━━━━━━━", norm["text"][:1500]]
    if norm.get("url"):
        lines.append(f"🔗 {norm['url']}")
    lines.append("──────────────")
    # Encrypted notices can't be replied to from here (we can't post encrypted).
    lines.append("🔒 Open Element to reply" if norm.get("encrypted") else "↩️ Reply to this message to respond")
    return "\n".join(lines)


# --- delivery + cursor ------------------------------------------------------

def _prune(db: Session) -> None:
    cutoff = datetime.utcnow() - timedelta(days=_REPLY_MAP_TTL_DAYS)
    db.query(SocialReplyMap).filter(SocialReplyMap.created_at < cutoff).delete(synchronize_session=False)


async def _deliver(db: Session, tg: TelegramService, user: User, chat_id: str, norm: dict) -> None:
    resp = await tg.send_message(chat_id, _format(norm), parse_mode="")
    msg_id = (resp or {}).get("result", {}).get("message_id")
    if not msg_id:
        logger.warning(f"[social] telegram send returned no message_id for user {user.id}: {resp}")
        return
    if norm.get("encrypted"):
        return  # informational only — no reply target to map
    db.add(SocialReplyMap(
        user_id=user.id,
        telegram_chat_id=chat_id,
        telegram_message_id=msg_id,
        platform=norm["platform"],
        target_id=norm.get("reply_target"),
        room_id=norm.get("room_id"),
        event_id=norm.get("event_id"),
        visibility=norm.get("visibility"),
    ))
    # Commit each mapping right after its message is sent, so a mid-batch failure can't
    # lose the mapping for an already-delivered message (or bleed it into a later commit).
    db.commit()


async def _relay_pleroma(db: Session, tg: TelegramService, user: User, chat_id: str) -> None:
    raw = await pleroma_service.fetch_notifications(
        user.pleroma_instance_url, user.pleroma_access_token, since_id=user.pleroma_notif_since
    )
    if not raw:
        return
    newest_id = raw[0].get("id")  # API returns newest-first
    if not user.pleroma_notif_since:
        # First poll: establish the cursor without forwarding the backlog.
        user.pleroma_notif_since = newest_id
        db.commit()
        return
    for n in reversed(raw):       # deliver oldest-first so chat order is chronological
        await _deliver(db, tg, user, chat_id, _norm_pleroma(n))
    user.pleroma_notif_since = newest_id
    _prune(db)
    db.commit()


async def _relay_misskey(db: Session, tg: TelegramService, user: User, chat_id: str) -> None:
    raw = await misskey_service.fetch_notifications(
        user.misskey_instance_url, user.misskey_api_token, since_id=user.misskey_notif_since
    )
    if not raw:
        return
    newest_id = raw[0].get("id")
    if not user.misskey_notif_since:
        # First poll: establish the cursor without forwarding the backlog.
        user.misskey_notif_since = newest_id
        db.commit()
        return
    for n in reversed(raw):
        await _deliver(db, tg, user, chat_id, _norm_misskey(n))
    user.misskey_notif_since = newest_id
    _prune(db)
    db.commit()


async def _relay_matrix(db: Session, tg: TelegramService, user: User, chat_id: str) -> None:
    events, next_batch = await matrix_service.fetch_notifications(
        user.matrix_homeserver, user.matrix_access_token, user.matrix_user_id, since=user.matrix_notif_since
    )
    for ev in events:
        await _deliver(db, tg, user, chat_id, _norm_matrix(ev))
    cursor_changed = bool(next_batch) and next_batch != user.matrix_notif_since
    if events or cursor_changed:
        if next_batch:
            user.matrix_notif_since = next_batch
        _prune(db)
        db.commit()


async def _poll_user(db: Session, tg: TelegramService, user: User) -> None:
    chat_id = str(user.telegram_chat_id)
    if user.pleroma_enabled and user.pleroma_instance_url and user.pleroma_access_token:
        try:
            await _relay_pleroma(db, tg, user, chat_id)
        except Exception as e:
            logger.warning(f"[social] pleroma relay failed for user {user.id}: {e}")
    if user.misskey_enabled and user.misskey_instance_url and user.misskey_api_token:
        try:
            await _relay_misskey(db, tg, user, chat_id)
        except Exception as e:
            logger.warning(f"[social] misskey relay failed for user {user.id}: {e}")
    if user.matrix_enabled and user.matrix_homeserver and user.matrix_access_token and user.matrix_user_id:
        try:
            await _relay_matrix(db, tg, user, chat_id)
        except Exception as e:
            logger.warning(f"[social] matrix relay failed for user {user.id}: {e}")


async def poll_once(db: Session) -> None:
    """Poll all eligible users once. The per-user toggle is the real control; the global
    setting is an admin kill-switch that is ON unless explicitly set to "false"."""
    if _get_setting(db, "social_notif_enabled", "true").lower() == "false":
        return
    tg = _build_telegram(db)
    if not tg:
        return
    users = (
        db.query(User)
        .filter(User.social_notif_enabled == True, User.telegram_chat_id.isnot(None))  # noqa: E712
        .all()
    )
    for user in users:
        try:
            await _poll_user(db, tg, user)
        except Exception as e:
            logger.warning(f"[social] poll failed for user {user.id}: {e}")
            db.rollback()


# --- reply-back -------------------------------------------------------------

async def handle_reply(db: Session, chat_id, reply_to_message_id: int, text: str) -> Optional[str]:
    """If the replied-to Telegram message maps to a forwarded notification, post `text` as a
    reply on that platform. Returns a confirmation string, or None if there's no mapping
    (so the caller can fall through to normal command/chat handling)."""
    row = (
        db.query(SocialReplyMap)
        .filter(
            SocialReplyMap.telegram_chat_id == str(chat_id),
            SocialReplyMap.telegram_message_id == reply_to_message_id,
        )
        .order_by(SocialReplyMap.id.desc())
        .first()
    )
    if not row:
        return None
    user = db.query(User).filter(User.id == row.user_id).first()
    if not user:
        return None
    try:
        if row.platform == "pleroma":
            if not row.target_id:
                return "⚠️ That notification has nothing to reply to."
            await pleroma_service.post_status(
                user.pleroma_instance_url, user.pleroma_access_token, text,
                visibility=row.visibility or "public", in_reply_to_id=row.target_id,
            )
            return "✅ Reply posted to Pleroma."
        if row.platform == "misskey":
            if not row.target_id:
                return "⚠️ That notification has nothing to reply to."
            await misskey_service.post_note(
                user.misskey_instance_url, user.misskey_api_token, text,
                visibility=row.visibility or "public", reply_id=row.target_id,
            )
            return "✅ Reply posted to Misskey."
        if row.platform == "matrix":
            if not row.room_id:
                return "⚠️ That notification has no room to reply to."
            await matrix_service.send_message(
                user.matrix_homeserver, user.matrix_access_token, row.room_id, text,
            )
            return "✅ Reply sent to the Matrix room."
    except Exception as e:
        logger.warning(f"[social] reply failed (platform={row.platform}, user={user.id}): {e}")
        return f"❌ Failed to send reply: {e}"
    return None


# --- scheduler --------------------------------------------------------------

_scheduler = None


def start_social_notifications_scheduler() -> None:
    """Start the interval poller (idempotent). Must be called from within a running event
    loop (e.g. FastAPI startup), like start_logs_scheduler."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        try:
            secs = int(_get_setting(db, "social_notif_poll_seconds", "60") or "60")
        except ValueError:
            secs = 60
    finally:
        db.close()
    secs = max(15, secs)

    async def _job():
        from app.database import SessionLocal as _SL
        _db = _SL()
        try:
            await poll_once(_db)
        except Exception as e:
            logger.warning(f"[social] poll job error: {e}")
        finally:
            _db.close()

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=secs, id="social_notif_poll", max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info(f"[social] notification poller started (every {secs}s)")
