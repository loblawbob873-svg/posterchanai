"""Relay each user's Pleroma/Misskey notifications to a private Matrix DM with the bot.

The Matrix counterpart of social_notifications_service (which relays to Telegram). A background
poller (started from app.main on port 3051) DMs the user — via the fedi-timeline bot account —
every new notification (mention, reply, favourite, boost, follow). It reuses the social relay's
normalization and the platform fetch clients, but renders a Matrix-native message (clean HTML,
no Telegram "reply to respond" hint) and keeps its OWN per-user cursors so the two relays don't
consume each other's notifications.

Gating: global `matrix_notif_enabled` Setting (admin kill-switch, default off) + the user's own
per-user `matrix_notif_enabled` opt-in (independent of the Telegram relay's `social_notif_enabled`)
+ a linked Matrix account (matrix_user_id) + a linked fedi account.
State is per-user in UserSetting (cursor + DM room id); the poller is per-process (port 3051).
"""
import html
import logging

from sqlalchemy.orm import Session

from app.models import User, Setting, UserSetting
from app.services import misskey_service, pleroma_service, matrix_service
from app.services.social_notifications_service import _norm_pleroma, _norm_misskey

logger = logging.getLogger(__name__)


# Notification type → (icon, human phrase). Covers Pleroma/Mastodon and Misskey type names.
_NOTIF_LABELS = {
    "mention": ("💬", "mentioned you"),
    "reply": ("↩️", "replied to you"),
    "favourite": ("⭐", "favourited your post"),
    "reaction": ("⭐", "reacted to your post"),
    "pleroma:emoji_reaction": ("⭐", "reacted to your post"),
    "reblog": ("🔁", "boosted your post"),
    "renote": ("🔁", "boosted your post"),
    "quote": ("🗣️", "quoted your post"),
    "follow": ("➕", "followed you"),
    "follow_request": ("➕", "requested to follow you"),
    "receiveFollowRequest": ("➕", "requested to follow you"),
    "poll": ("📊", "a poll you voted in ended"),
    "pollEnded": ("📊", "a poll ended"),
}


def _format_notification(norm: dict, avatar_mxc: str | None = None) -> tuple[str, str]:
    """Render a notification to (plain_body, html). We build the HTML by hand (escaping handles
    like @no_de_score so underscores aren't turned into italics) rather than markdown-rendering.
    When avatar_mxc is given, the actor's avatar is inlined so you can see who interacted."""
    icon, phrase = _NOTIF_LABELS.get(norm.get("type"), ("🔔", norm.get("type") or "notification"))
    actor = norm.get("actor") or "Someone"            # @handle
    display = (norm.get("actor_display") or actor).strip()
    text = (norm.get("text") or "").strip()
    snippet = text if len(text) <= 280 else text[:279] + "…"

    plain = f"{icon} {display} ({actor}) {phrase}" if display != actor else f"{icon} {actor} {phrase}"
    av = f'<img src="{html.escape(avatar_mxc)}" width="20" height="20" /> ' if avatar_mxc else ""
    who = f'<strong>{html.escape(display)}</strong>'
    if display != actor:
        who += f' <font data-mx-color="#888888">{html.escape(actor)}</font>'
    parts = [f'{icon} {av}{who} {html.escape(phrase)}']
    if snippet:
        plain += f"\n\n“{snippet}”"
        parts.append(f"<blockquote>{html.escape(snippet).replace(chr(10), '<br>')}</blockquote>")
    # No 'Open thread' web link: Element auto-generates a bulky URL-preview card from it
    # (author avatar + bio/post text) that duplicates the message and is hard to read. The
    # conversation is already mirrored into this notification's Matrix thread for full context.
    if norm.get("reply_target"):
        # Reply-back is wired for Matrix DMs (matrix_notifications_service + /notification-reply).
        plain += "\n↩️ Reply to this message to respond"
        parts.append("<em>↩️ Reply to this message to respond</em>")
    return plain, "<br>".join(parts)


# --- settings / state helpers -----------------------------------------------

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


async def _dm_room(db: Session, hs: str, bot_token: str, user: User) -> str | None:
    """The user's notification DM room id, creating + persisting it on first use."""
    room_id = _get_user_setting(db, user.id, "matrix_notif_dm_room")
    if room_id:
        return room_id
    try:
        room_id = await matrix_service.create_dm_room_with(hs, bot_token, user.matrix_user_id)
    except Exception as e:
        logger.warning(f"[matrix-notif] could not create DM room for user {user.id}: {e}")
        return None
    _set_user_setting(db, user.id, "matrix_notif_dm_room", room_id)
    db.commit()
    return room_id


# --- per-platform relay -----------------------------------------------------

_MAX_CONTEXT = 25   # cap conversation messages mirrored into a notification thread


async def _thread_context(db: Session, hs: str, bot_token: str, room_id: str, root_event_id: str,
                          platform: str, instance_url: str, token: str, status_id: str) -> None:
    """Mirror the notified post's FULL conversation — ancestors + the post itself + descendants —
    into the notification's Matrix thread, so the user reads the whole thing in Element.

    Note: we dedup only within THIS thread (a local set), not against the room-wide _seen index.
    The room-wide check would drop any reply/post that had appeared in another notification's
    thread or the timeline, leaving gaps — and fetch_context never returns the notified post
    itself, so it would otherwise be missing entirely. Each notification thread is self-contained."""
    from app.services import fedi_timeline_service as ftl
    try:
        if platform == "pleroma":
            ctx = await pleroma_service.fetch_context(instance_url, token, status_id)
            note = await pleroma_service.fetch_status(instance_url, token, status_id)
            raw_items = (ctx.get("ancestors") or []) + ([note] if note else []) + (ctx.get("descendants") or [])
        else:
            anc = await misskey_service.fetch_conversation(instance_url, token, status_id)
            note = await misskey_service.call(instance_url, token, "notes/show", {"noteId": status_id})
            kids = await misskey_service.fetch_children(instance_url, token, status_id)
            # Misskey returns ancestors nearest-first; reverse to oldest-first so each reply is
            # posted after its parent (lets _deliver thread it under the right message).
            raw_items = list(reversed(anc or [])) + ([note] if note else []) + (kids or [])
    except Exception as e:
        logger.warning(f"[matrix-notif] context fetch failed: {e}")
        return
    seen_local: set[str] = set()
    for raw in raw_items[:_MAX_CONTEXT]:
        post = ftl._norm(platform, raw)
        pid = post.get("id")
        if not pid or pid in seen_local:
            continue
        seen_local.add(pid)
        try:
            await ftl._deliver(db, hs, bot_token, room_id, platform, instance_url, post,
                               thread_root_event_id=root_event_id)
        except Exception as e:
            logger.warning(f"[matrix-notif] context deliver failed: {e}")


async def _relay(db: Session, hs: str, bot_token: str, user: User, room_id: str,
                 platform: str, instance_url: str, token: str, raw: list, normalize) -> None:
    """Deliver new notifications oldest-first; first poll only sets the cursor (no backfill).
    For each delivered notification that concerns a post, record a MatrixNotifyMap row so the
    user can reply to the DM message and have it post back to the fediverse."""
    from app.models import MatrixNotifyMap
    if not raw:
        return
    cursor_key = f"matrix_notif_{platform}_since"
    since = _get_user_setting(db, user.id, cursor_key)
    newest_id = raw[0].get("id")        # platform APIs return newest-first
    if not since:
        _set_user_setting(db, user.id, cursor_key, newest_id)
        db.commit()
        return
    for n in reversed(raw):
        norm = normalize(n)
        # Misskey notifications carry no URL; build a viewable note link for context.
        if platform == "misskey" and not norm.get("url") and norm.get("reply_target"):
            norm["url"] = f"{instance_url.rstrip('/')}/notes/{norm['reply_target']}"
        # Inline the actor's avatar so you can see WHO interacted (the DM is sent by the bot
        # account, so the message sender avatar is the bot, not the actor).
        avatar_mxc = None
        try:
            from app.services import fedi_timeline_service as _ftl
            avatar_mxc = await _ftl._avatar_mxc(db, hs, bot_token, norm.get("actor_avatar"))
        except Exception as e:
            logger.warning(f"[matrix-notif] actor avatar upload failed: {e}")
        plain, html_body = _format_notification(norm, avatar_mxc)
        try:
            event_id = await matrix_service.send_event(hs, bot_token, room_id, plain, html=html_body)
        except Exception as e:
            # Stop here, but the cursor is already advanced past everything sent so far (each
            # success commits below), so we never re-send delivered notifications — only the
            # unsent tail is retried next poll. This is what prevents the duplicate flood.
            logger.warning(f"[matrix-notif] send failed for user {user.id}: {e}")
            return
        # Record the reply target (mentions/replies → the other person's post) so a reply to
        # this DM message posts back. Skip types with nothing to reply to (e.g. follows).
        if event_id and norm.get("reply_target"):
            db.add(MatrixNotifyMap(
                user_id=user.id, room_id=room_id, event_id=event_id, platform=platform,
                instance_url=instance_url, target_id=norm["reply_target"],
                visibility=norm.get("visibility"),
            ))
            db.commit()
            # Mirror the conversation into this notification's thread (view in Element, not web).
            await _thread_context(db, hs, bot_token, room_id, event_id, platform,
                                  instance_url, token, norm["reply_target"])
        # Advance the cursor per delivered notification (not once at the end) so a mid-batch
        # failure can't cause already-sent ones to be redelivered.
        if n.get("id"):
            _set_user_setting(db, user.id, cursor_key, n["id"])
        db.commit()


async def _poll_user(db: Session, hs: str, bot_token: str, user: User) -> None:
    room_id = await _dm_room(db, hs, bot_token, user)
    if not room_id:
        return
    if user.pleroma_enabled and user.pleroma_instance_url and user.pleroma_access_token:
        try:
            raw = await pleroma_service.fetch_notifications(
                user.pleroma_instance_url, user.pleroma_access_token,
                since_id=_get_user_setting(db, user.id, "matrix_notif_pleroma_since") or None,
            )
            await _relay(db, hs, bot_token, user, room_id, "pleroma", user.pleroma_instance_url,
                         user.pleroma_access_token, raw, _norm_pleroma)
        except Exception as e:
            logger.warning(f"[matrix-notif] pleroma poll failed for user {user.id}: {e}")
    if user.misskey_enabled and user.misskey_instance_url and user.misskey_api_token:
        try:
            raw = await misskey_service.fetch_notifications(
                user.misskey_instance_url, user.misskey_api_token,
                since_id=_get_user_setting(db, user.id, "matrix_notif_misskey_since") or None,
            )
            await _relay(db, hs, bot_token, user, room_id, "misskey", user.misskey_instance_url,
                         user.misskey_api_token, raw, _norm_misskey)
        except Exception as e:
            logger.warning(f"[matrix-notif] misskey poll failed for user {user.id}: {e}")


async def poll_once(db: Session) -> None:
    # The timeline bridge is the master switch for ALL fedi->Matrix output (it owns the bot
    # credentials these DMs reuse): when it's off, the personal notification DMs stop too, even if
    # their own toggle is on.
    if _get_setting(db, "fedi_timeline_enabled", "false").lower() != "true":
        return
    if _get_setting(db, "matrix_notif_enabled", "false").lower() != "true":
        return
    hs = _get_setting(db, "fedi_timeline_matrix_homeserver").rstrip("/")
    bot_token = _get_setting(db, "fedi_timeline_matrix_bot_token")
    if not (hs and bot_token):
        return
    users = (
        db.query(User)
        .filter(User.matrix_notif_enabled == True, User.matrix_user_id.isnot(None))  # noqa: E712
        .all()
    )
    for user in users:
        try:
            await _poll_user(db, hs, bot_token, user)
        except Exception as e:
            logger.warning(f"[matrix-notif] poll failed for user {user.id}: {e}")
            db.rollback()


# --- scheduler --------------------------------------------------------------

_scheduler = None


def start_matrix_notifications_scheduler() -> None:
    """Start the interval poller (idempotent), like start_social_notifications_scheduler."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        try:
            secs = int(_get_setting(db, "matrix_notif_poll_seconds", "60") or "60")
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
            logger.warning(f"[matrix-notif] poll job error: {e}")
            _db.rollback()
        finally:
            _db.close()

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=secs, id="matrix_notif_poll", max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info(f"[matrix-notif] Matrix DM notification poller started (every {secs}s)")


def stop_matrix_notifications_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception as e:
            logger.warning(f"[matrix-notif] scheduler shutdown error: {e}")
        _scheduler = None
