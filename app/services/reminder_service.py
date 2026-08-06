"""Reminders — the `remind` command.

Flow:
  1. The user types natural language: `remind open the oven in 10m`, `remind me next tuesday to
     open the oven`. The LLM (`parse_reminder`) turns it into {text, due_at(UTC)}.
  2. The row is stored (status="pending").
  3. A background AsyncIOScheduler (`start_reminder_scheduler`, port-3051 only) polls every
     ~30s for due pending reminders and delivers them, then marks them "done".

Delivery (per the product flow): ALWAYS to the web UI — a dedicated "⏰ Reminders" conversation
plus a best-effort live websocket push to any connected client — and ALSO to Telegram when the
user has Telegram configured.

Times are handled in UTC (the model convention, matching `datetime.utcnow()`); relative phrases
("in 10m", "in 2 hours") are exact in any timezone, which is the common case.
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models import User, Conversation, Message, Reminder

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

logger = logging.getLogger("reminder_service")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('%(asctime)s [REMIND] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(_h)

REMINDERS_CHAT_TITLE = "⏰ Reminders"

# Fallback relative parser used when the LLM is unavailable or returns nothing usable.
_UNIT_PAT = r'(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)'
# "… in 10m …" anywhere in the text.
_REL_RE = re.compile(r'\bin\s+(\d+)\s*' + _UNIT_PAT + r'\b', re.IGNORECASE)
# A bare trailing duration with no "in", e.g. "open oven 10m" / "call mom 2 hours" — the common
# way people phrase it. Anchored to the END so a number mid-sentence isn't mistaken for a time.
_REL_END_RE = re.compile(r'^(.*?)[\s,]*\b(\d+)\s*' + _UNIT_PAT + r'\s*$', re.IGNORECASE)
_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
}


def _strip_lead(text: str) -> str:
    """Drop a leading 'me to'/'me'/'to' so the stored text reads as the task itself."""
    t = (text or "").strip()
    t = re.sub(r'^(me\s+to\s+|me\s+|to\s+)', '', t, flags=re.IGNORECASE)
    return t.strip()


def _fallback_parse(text: str, now: datetime) -> Optional[dict]:
    """Regex fallback for a relative duration — returns {text, due_at} or None.
    Handles both "open oven in 10m" (in-anywhere) and "open oven 10m" (bare trailing duration)."""
    text = text or ""
    m = _REL_RE.search(text)
    if m:
        secs = int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
        body = (text[:m.start()] + " " + text[m.end():]).strip()
        return {"text": _strip_lead(body) or "Reminder", "due_at": now + timedelta(seconds=secs)}
    m = _REL_END_RE.match(text)
    if m:
        secs = int(m.group(2)) * _UNIT_SECONDS[m.group(3).lower()]
        return {"text": _strip_lead(m.group(1)) or "Reminder", "due_at": now + timedelta(seconds=secs)}
    return None


def _get_setting_value(db: Session, user_id: int, key: str) -> Optional[str]:
    from app.models import UserSetting
    s = (db.query(UserSetting)
         .filter(UserSetting.user_id == user_id, UserSetting.key == key)
         .first())
    return s.value if s else None


def get_user_tzinfo(db: Session, user_id: int):
    """The user's timezone, auto-detected from their browser (no manual entry). Prefers the IANA
    zone name (e.g. "Asia/Bangkok" — DST-aware), falling back to the stored numeric UTC offset, and
    finally UTC. Stored via POST /api/auth/timezone on every web page load, so it follows the user
    when they travel; Telegram-only reminders reuse whatever the web last reported."""
    name = _get_setting_value(db, user_id, "tz_name")
    if name and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    off = _get_setting_value(db, user_id, "tz_offset_minutes")
    try:
        if off is not None:
            return timezone(timedelta(minutes=int(off)))
    except (ValueError, TypeError):
        pass
    return timezone.utc


async def parse_reminder(text: str, chat_service, now: Optional[datetime] = None, tz=None) -> dict:
    """Parse a natural-language reminder into {ok, text, due_at(naive UTC), error}.

    A clear relative phrase ("in 10s/10m/2h/3d") is parsed EXACTLY by regex first — fast and not
    subject to LLM rounding (the "in 10s became 10 min" bug). Anything else (absolute/fuzzy:
    "tomorrow", "next tuesday 9am") goes to the LLM, interpreted in the user's local timezone `tz`
    and converted to UTC for storage."""
    now = now or datetime.utcnow()              # naive UTC
    tz = tz or timezone.utc
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Tell me what to remind you about, e.g. `remind open the oven in 10m`."}

    # 1) Exact relative ("in N unit") — timezone-independent, no LLM.
    parsed = _fallback_parse(text, now)

    # 2) Otherwise ask the LLM, interpreting in the user's local time.
    if not parsed:
        try:
            local_now = now.replace(tzinfo=timezone.utc).astimezone(tz)
            prompt = (
                "You convert a reminder request into JSON. The user's current LOCAL time is "
                f"{local_now.strftime('%Y-%m-%dT%H:%M:%S')} ({local_now.strftime('%A')}).\n"
                "Return ONLY a JSON object, no prose, with exactly these keys:\n"
                '  "text": the thing to be reminded of, phrased as a short imperative WITHOUT '
                '"remind me" (e.g. "open the oven").\n'
                '  "iso": the absolute due time as "YYYY-MM-DDTHH:MM:SS" in the user\'s LOCAL time, '
                "computed from their current local time (tomorrow, next tuesday 9am, at 18:00). "
                "Use null if no time is present.\n"
                f"Request: {text}"
            )
            raw = (await chat_service.chat([{"role": "user", "content": prompt}]) or "").strip()
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                obj = json.loads(m.group(0))
                iso = obj.get("iso")
                body = _strip_lead(obj.get("text") or "")
                if iso:
                    # LLM gave LOCAL wall-clock time → localize in tz, then convert to naive UTC.
                    local_due = datetime.fromisoformat(str(iso).replace("Z", "").strip())
                    local_due = local_due.replace(tzinfo=tz)
                    due_utc = local_due.astimezone(timezone.utc).replace(tzinfo=None)
                    parsed = {"text": body or text, "due_at": due_utc}
        except Exception as e:
            logger.info(f"LLM parse failed ({e})")

    if not parsed:
        return {"ok": False, "error": (
            "I couldn't work out *when* to remind you. Try a clear time, e.g. "
            "`remind open the oven in 10m` or `remind me next tuesday to open the oven`.")}

    # Guard against times in the past (clock drift / ambiguous parse).
    if parsed["due_at"] <= now:
        parsed["due_at"] = now + timedelta(seconds=5)
    return {"ok": True, "text": parsed["text"], "due_at": parsed["due_at"]}


# --------------------------------------------------------------------------- CRUD

def create_reminder(db: Session, user: User, text: str, due_at: datetime) -> Reminder:
    r = Reminder(user_id=user.id, text=text, due_at=due_at, status="pending")
    db.add(r)
    db.commit()
    db.refresh(r)
    from app.services import record_store
    record_store.mirror_reminder_blocking(db, user, r)
    return r


def list_reminders(db: Session, user: User) -> list:
    return (db.query(Reminder)
            .filter(Reminder.user_id == user.id, Reminder.status == "pending")
            .order_by(Reminder.due_at.asc())
            .all())


def get_reminder(db: Session, user: User, rid: int) -> Optional[Reminder]:
    return (db.query(Reminder)
            .filter(Reminder.id == rid, Reminder.user_id == user.id)
            .first())


def cancel_reminder(db: Session, user: User, rid: int) -> bool:
    r = get_reminder(db, user, rid)
    if not r or r.status != "pending":
        return False
    r.status = "cancelled"
    db.commit()
    from app.services import record_store
    record_store.mirror_reminder_blocking(db, user, r)
    return True


def snooze_reminder(db: Session, user: User, rid: int, minutes: int) -> Optional[Reminder]:
    r = get_reminder(db, user, rid)
    if not r:
        return None
    base = max(r.due_at, datetime.utcnow())
    r.due_at = base + timedelta(minutes=minutes)
    r.status = "pending"
    r.delivered_at = None
    db.commit()
    db.refresh(r)
    from app.services import record_store
    record_store.mirror_reminder_blocking(db, user, r)
    return r


# --------------------------------------------------------------------------- formatting

def humanize_due(due_at: datetime, now: Optional[datetime] = None, tz=None) -> str:
    """A short 'in 10 minutes' / 'in 2 days' phrase plus the absolute time in the user's LOCAL
    timezone (``due_at`` is stored naive UTC; ``tz`` is a tzinfo)."""
    now = now or datetime.utcnow()
    tz = tz or timezone.utc
    delta = (due_at - now).total_seconds()
    if delta < 0:
        rel = "now"
    elif delta < 90:
        rel = f"in {int(delta)}s"
    elif delta < 5400:
        rel = f"in {round(delta / 60)} min"
    elif delta < 172800:
        rel = f"in {round(delta / 3600)} h"
    else:
        rel = f"in {round(delta / 86400)} days"
    local = due_at.replace(tzinfo=timezone.utc).astimezone(tz)
    return f"{rel} ({local.strftime('%Y-%m-%d %H:%M %Z')})"


# --------------------------------------------------------------------------- delivery

def _get_or_create_reminders_chat(db: Session, user_id: int) -> Conversation:
    chat = (db.query(Conversation)
            .filter(Conversation.user_id == user_id, Conversation.title == REMINDERS_CHAT_TITLE)
            .first())
    if chat:
        return chat
    chat = Conversation(user_id=user_id, title=REMINDERS_CHAT_TITLE)
    db.add(chat)
    db.commit()
    db.refresh(chat)
    return chat


async def deliver(db: Session, reminder: Reminder) -> None:
    """Deliver a fired reminder: always to the web UI, plus Telegram if configured."""
    user = db.query(User).filter(User.id == reminder.user_id).first()
    if not user:
        return
    body = f"⏰ Reminder: {reminder.text}"

    # Web UI (always): persist into the "⏰ Reminders" conversation so it's there whenever the
    # user looks, then best-effort push live to a connected websocket.
    chat = _get_or_create_reminders_chat(db, user.id)
    from app.services import chat_history
    await chat_history.append(db, user, chat.id, "assistant", body)   # encrypted event, no plaintext row
    chat.updated_at = datetime.utcnow()
    db.commit()
    try:
        from app.routers.chat import manager
        await manager.send_json(user.id, {
            "type": "reminder",
            "content": body,
            "reminder_id": reminder.id,
            "conversation_id": chat.id,
        })
    except Exception as e:
        logger.info(f"live push skipped: {e}")

    # Web Push / UnifiedPush — the only path that reaches a phone whose screen is OFF.
    #
    # The websocket above only lands if the app is open, and the chat row only if the user goes
    # looking. So a reminder set on a phone, for a phone, arrived nowhere at the moment it was due
    # unless Telegram happened to be linked — which is the one job a reminder has.
    #
    # Best-effort and last, deliberately: the reminder has already been claimed and recorded by this
    # point, so a push service having a bad day must not cost the delivery.
    npub = (getattr(user, "nostr_npub", "") or "").strip()
    if npub:
        try:
            from app.models import PushSubscription
            from app.services import push_service
            from app.services.nostr import nostr_service
            pk = nostr_service.to_pubkey_hex(npub)
            rows = db.query(PushSubscription).filter(PushSubscription.pubkey == pk).all() if pk else []
            payload = {"title": "⏰ Reminder", "body": reminder.text, "type": "reminder"}
            for row in rows:
                sub = {"endpoint": row.endpoint, "keys": {"p256dh": row.p256dh, "auth": row.auth}}
                if not await asyncio.to_thread(push_service.send, sub, payload):
                    db.delete(row)        # endpoint is gone for good — prune it
            if rows:
                db.commit()
        except Exception as e:
            logger.warning(f"reminder push failed for user {user.id}: {e}")

    # Telegram (only if configured for this user). Plain parse_mode + a bold, bordered banner so it
    # stands out in the chat list (and avoids Markdown parse errors on arbitrary reminder text).
    if getattr(user, "telegram_enabled", False) and getattr(user, "telegram_chat_id", None):
        try:
            from app.services.telegram_service import telegram_service, configure_from_settings
            configure_from_settings(db)
            tg_body = f"🔔🔔🔔 REMINDER 🔔🔔🔔\n━━━━━━━━━━━━━━\n⏰ {reminder.text}\n━━━━━━━━━━━━━━"
            await telegram_service.send_message(user.telegram_chat_id, tg_body, parse_mode="")
        except Exception as e:
            logger.warning(f"telegram delivery failed for user {user.id}: {e}")


# --------------------------------------------------------------------------- scheduler

async def poll_once(db: Session) -> None:
    """Fire any reminders whose due time has passed — each EXACTLY once.

    Each row is *claimed* with an atomic conditional UPDATE (pending → done) BEFORE delivery, so a
    reminder can never be delivered twice — even if a poll overlaps, the process restarts, or the
    row is somehow seen again, only the single UPDATE that flips it off "pending" wins and proceeds.
    (Trade-off: if delivery itself errored after the claim we'd skip it rather than risk a double —
    the web-UI insert is the reliable part and Telegram failures are caught inside `deliver`.)"""
    now = datetime.utcnow()
    due_ids = [r.id for r in (db.query(Reminder.id)
                              .filter(Reminder.status == "pending", Reminder.due_at <= now)
                              .order_by(Reminder.due_at.asc())
                              .limit(50)
                              .all())]
    for rid in due_ids:
        claimed = (db.query(Reminder)
                   .filter(Reminder.id == rid, Reminder.status == "pending")
                   .update({"status": "done", "delivered_at": datetime.utcnow()},
                           synchronize_session=False))
        db.commit()
        if not claimed:
            continue  # another claim already took it — never deliver twice
        r = db.query(Reminder).filter(Reminder.id == rid).first()
        try:
            from app.services import record_store
            if record_store.enabled(db):
                u = db.query(User).filter(User.id == r.user_id).first()
                if u:
                    await record_store.mirror_reminder(db, u, r)   # persist the done/delivered state
        except Exception as e:
            logger.debug(f"reminder mirror (delivered) failed: {e}")
        try:
            await deliver(db, r)
            logger.info(f"delivered reminder #{rid} to user {r.user_id}")
        except Exception as e:
            logger.warning(f"failed to deliver reminder #{rid} (already marked done, won't retry): {e}")


_scheduler = None


def start_reminder_scheduler() -> None:
    """Start the due-reminder poller (idempotent). Call from within the running event loop on the
    port-3051 instance only (like the other schedulers)."""
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    async def _job():
        from app.database import SessionLocal
        _db = SessionLocal()
        try:
            await poll_once(_db)
        except Exception as e:
            logger.warning(f"poll job error: {e}")
        finally:
            _db.close()

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=15, id="reminder_poll", max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("reminder poller started (every 15s)")


def stop_reminder_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
        logger.info("reminder poller stopped")
