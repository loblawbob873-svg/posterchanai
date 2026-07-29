"""Relay Pleroma notifications to a user's Telegram chat, and post
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

import json
from app.models import User, SocialReplyMap, UserSetting
from app.services import pleroma_service
from app.services import settings_store
from app.services.nostr import nostr_service
from app.services.telegram_service import TelegramService

logger = logging.getLogger(__name__)

_REPLY_MAP_TTL_DAYS = 7
_NOTIF_PAGE = 20            # per-page fetch size when draining notifications
_NOTIF_DRAIN_PAGES = 25     # max pages drained per poll (bound; leftover drains next cycle)
_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*br\s*/?\s*>|</\s*p\s*>", re.IGNORECASE)


# --- settings helpers -------------------------------------------------------

def _get_setting(db: Session, key: str, default: str = "") -> str:
    return settings_store.get(key) or default


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




def _norm_pleroma(n: dict) -> dict:
    acct = n.get("account") or {}
    status = n.get("status") or {}
    actor_str = "@" + (acct.get("acct") or acct.get("username", "?"))
    return {
        "platform": "pleroma",
        "type": n.get("type", "notification"),
        "actor": actor_str,
        "actor_display": acct.get("display_name") or acct.get("username") or actor_str,
        "actor_avatar": acct.get("avatar"),
        "text": _strip_html(status.get("content", "")) if status else "",
        "reply_target": status.get("id") if status else None,
        "room_id": None,
        "event_id": None,
        "visibility": status.get("visibility", "public") if status else "public",
        "url": status.get("url") if status else None,
    }


_NOSTR_KIND_TYPE = {1: "mention", 6: "repost", 7: "reaction"}

# pubkey hex → display label (NIP-05 / profile name), resolved once from kind-0 metadata.
_nostr_name_cache: dict = {}


async def _nostr_actor_label(pubkey: str, relays) -> str:
    """Prefer the sender's NIP-05 (or profile name) over the raw npub in notifications.
    Resolved once per pubkey from kind-0 metadata and cached; npub is the fallback. Not
    verified against .well-known/nostr.json — a self-asserted handle is fine for display."""
    cached = _nostr_name_cache.get(pubkey)
    if cached:
        return cached
    try:
        npub = nostr_service.npub_of(pubkey)
    except Exception:
        npub = pubkey[:12]
    try:
        meta = await nostr_service.get_metadata(pubkey, relays)
    except Exception:
        return npub  # transient relay error — don't cache, retry next poll
    nip05 = (meta.get("nip05") or "").strip()
    if nip05:
        # NIP-05 "_@domain" is the root identity — show it as just the domain.
        label = nip05[2:] if nip05.startswith("_@") else nip05
    else:
        label = (meta.get("display_name") or meta.get("name") or "").strip() or npub
    _nostr_name_cache[pubkey] = label
    return label


def _norm_nostr(ev: dict, actor_label: Optional[str] = None) -> dict:
    """Normalize a raw Nostr event (kind 1 mention/reply, 6 repost, 7 reaction).
    `actor_label` (NIP-05/profile name) overrides the npub for display when resolved."""
    pubkey = ev.get("pubkey", "")
    try:
        npub = nostr_service.npub_of(pubkey)
    except Exception:
        npub = pubkey[:12]
    kind = ev.get("kind", 1)
    content = ev.get("content", "") or ""
    if kind == 7:
        text = f"reacted {content or '+'}"
    elif kind == 6:
        text = "reposted your note"
    else:
        text = content
    return {
        "platform": "nostr",
        "type": _NOSTR_KIND_TYPE.get(kind, "notification"),
        "actor": actor_label or npub,
        "actor_display": actor_label or (npub[:16] + "…"),
        "actor_avatar": None,
        "text": text,
        # Only kind-1 mentions/replies are a sensible reply target; reactions/reposts notify only.
        "reply_target": ev.get("id") if kind == 1 else None,
        "room_id": None,
        "event_id": None,
        "visibility": "public",
        "url": None,
    }


_PLATFORM_ICON = {"pleroma": "💧", "nostr": "🟣"}


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


# A follow is a ONE-TIME event, but Pleroma re-issues the follow notification with a fresh id
# (on re-federation / notification grouping / a transient unfollow-refollow) — that new id slips past
# the since_id cursor, so a follow from days ago gets re-announced. Dedup follows by ACTOR, persisted
# per user (UserSetting), so a given account's follow is announced exactly once per relay.
_FOLLOW_TYPES = {"follow", "follow_request", "followRequestAccepted", "receiveFollowRequest"}
_SEEN_FOLLOWS_CAP = 3000


def is_dupe_follow(db: Session, user: User, norm: dict, store_key: str = "social_notif_seen_follows") -> bool:
    """True (→ caller should skip) if this follow notification's actor was already announced to `user`.
    No-op for non-follow types. `store_key` lets each relay keep its own seen-set
    so both still notify the follow once. Records the actor on first sight."""
    if (norm.get("type") or "") not in _FOLLOW_TYPES:
        return False
    fkey = f"{norm.get('platform')}:{norm.get('actor')}"
    row = db.query(UserSetting).filter(UserSetting.user_id == user.id, UserSetting.key == store_key).first()
    try:
        seen = set(json.loads(row.value)) if (row and row.value) else set()
    except Exception:
        seen = set()
    if fkey in seen:
        return True
    seen.add(fkey)
    if len(seen) > _SEEN_FOLLOWS_CAP:
        seen = set(sorted(seen)[-_SEEN_FOLLOWS_CAP:])
    val = json.dumps(sorted(seen))
    if row:
        row.value = val
    else:
        db.add(UserSetting(user_id=user.id, key=store_key, value=val))
    db.commit()
    return False


async def _deliver(db: Session, tg: TelegramService, user: User, chat_id: str, norm: dict) -> bool:
    """True = delivered (or an intentional dupe-skip) → the caller may advance the cursor past it.
    False = the Telegram send FAILED (429/network/etc.) → the caller must NOT advance past it or the
    notification is silently lost with no retry."""
    if is_dupe_follow(db, user, norm):
        return True  # a follow we've already announced (re-issued past the cursor) — safe to advance past
    resp = await tg.send_message(chat_id, _format(norm), parse_mode="")
    msg_id = (resp or {}).get("result", {}).get("message_id")
    if not msg_id:
        logger.warning(f"[social] telegram send FAILED for user {user.id} (cursor held, will retry): {resp}")
        return False
    if norm.get("encrypted"):
        return True  # informational only — no reply target to map
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
    return True


async def _relay_pleroma(db: Session, tg: TelegramService, user: User, chat_id: str) -> None:
    if not user.pleroma_notif_since:
        # First poll: establish the cursor without forwarding the backlog.
        raw = await pleroma_service.fetch_notifications(
            user.pleroma_instance_url, user.pleroma_access_token, limit=1)
        if raw:
            user.pleroma_notif_since = raw[0].get("id")
            db.commit()
        return
    # Drain forward from the cursor page-by-page with min_id (GAPLESS). A single since_id fetch drops
    # everything beyond one page when more than `limit` notifications arrive between polls (the
    # "missing a bunch" bug); min_id returns the items immediately after the cursor so nothing is lost.
    for _ in range(_NOTIF_DRAIN_PAGES):
        raw = await pleroma_service.fetch_notifications(
            user.pleroma_instance_url, user.pleroma_access_token,
            min_id=user.pleroma_notif_since, limit=_NOTIF_PAGE)
        if not raw:
            break
        last_ok = None
        for n in reversed(raw):       # API returns newest-first → deliver oldest-first (chronological)
            if not await _deliver(db, tg, user, chat_id, _norm_pleroma(n)):
                break                 # Telegram send failed → stop; advance only to the last delivered
            last_ok = n.get("id")
        if last_ok:                   # advance to the newest SUCCESSFULLY-delivered (not the newest fetched)
            user.pleroma_notif_since = last_ok
            _prune(db)
            try:
                db.commit()
            except Exception:         # poll txn killed (idle timeout) → persist the cursor in a fresh
                db.rollback()         # session so we don't re-deliver this whole page next poll
                from app.database import commit_in_fresh_session
                commit_in_fresh_session(lambda s: setattr(s.get(User, user.id), "pleroma_notif_since", last_ok))
        if not last_ok or len(raw) < _NOTIF_PAGE:   # send failure, or partial page (caught up) → stop draining
            break




def _nostr_cfg(user: User) -> tuple[bytes, list, dict]:
    """(seckey, relays, media_cfg) for a user's linked Nostr account."""
    seckey = nostr_service.decode_seckey(user.nostr_nsec)
    relays = nostr_service.relay.normalize_relays(user.nostr_relays) or nostr_service.DEFAULT_RELAYS
    media_cfg = {"service": user.nostr_media_service or "blossom",
                 "endpoint": user.nostr_media_endpoint or ""}
    return seckey, relays, media_cfg


async def _relay_nostr(db: Session, tg: TelegramService, user: User, chat_id: str) -> None:
    pubkey = nostr_service.derive_pubkey(nostr_service.decode_seckey(user.nostr_nsec))
    relays = nostr_service.relay.normalize_relays(user.nostr_relays) or nostr_service.DEFAULT_RELAYS
    since = int(user.nostr_notif_since) if (user.nostr_notif_since or "").isdigit() else None
    raw = await nostr_service.fetch_mentions(pubkey, relays, since=since)
    # Exclude the user's own events (e.g. our replies/reactions that carry a self p-tag).
    raw = [ev for ev in raw if ev.get("pubkey") != pubkey]
    if not raw:
        return
    newest = max(int(ev.get("created_at", 0)) for ev in raw)
    # Cursor is a unix-second timestamp; fetch_mentions queries `since+1` to avoid re-sending
    # the boundary event. Trade-off: a second mention landing in the SAME second as `newest`
    # after this poll is skipped (Nostr lacks the opaque ids Pleroma cursors on). Rare
    # for a single account; accepted over the duplicate-flood the inclusive alternative causes.
    if since is None:
        # First poll: establish the cursor without forwarding the backlog.
        user.nostr_notif_since = str(newest)
        db.commit()
        return
    for ev in sorted(raw, key=lambda e: e.get("created_at", 0)):  # oldest-first
        label = await _nostr_actor_label(ev.get("pubkey", ""), relays)
        await _deliver(db, tg, user, chat_id, _norm_nostr(ev, actor_label=label))
    user.nostr_notif_since = str(newest)
    _prune(db)
    db.commit()



async def _poll_user(db: Session, tg: TelegramService, user: User) -> None:
    chat_id = str(user.telegram_chat_id)
    if user.pleroma_enabled and user.pleroma_instance_url and user.pleroma_access_token:
        try:
            await _relay_pleroma(db, tg, user, chat_id)
        except Exception as e:
            logger.warning(f"[social] pleroma relay failed for user {user.id}: {e}")
    if getattr(user, "nostr_enabled", False) and user.nostr_nsec:
        try:
            await _relay_nostr(db, tg, user, chat_id)
        except Exception as e:
            logger.warning(f"[social] nostr relay failed for user {user.id}: {e}")


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
        if row.platform == "nostr":
            if not row.target_id:
                return "⚠️ That notification has nothing to reply to."
            seckey, relays, media_cfg = _nostr_cfg(user)
            parent = await nostr_service.fetch_event(relays, row.target_id)
            # If the relays don't return the parent event, still thread the reply off its id
            # (e root tag) instead of posting a detached note that loses the conversation.
            if not parent:
                parent = {"id": row.target_id, "pubkey": "", "tags": []}
            await nostr_service.post_note(seckey, relays, text, reply_to=parent, media_cfg=media_cfg)
            return "✅ Reply posted to Nostr."
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
