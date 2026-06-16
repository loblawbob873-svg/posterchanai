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
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models import User, Conversation, Message, Reminder

logger = logging.getLogger("reminder_service")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter('%(asctime)s [REMIND] %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
    logger.addHandler(_h)

REMINDERS_CHAT_TITLE = "⏰ Reminders"

# Fallback "in <n> <unit>" parser used when the LLM is unavailable or returns nothing usable.
_REL_RE = re.compile(
    r'\bin\s+(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)\b',
    re.IGNORECASE,
)
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
    """Regex fallback for 'in <n> <unit>' — returns {text, due_at} or None."""
    m = _REL_RE.search(text or "")
    if not m:
        return None
    secs = int(m.group(1)) * _UNIT_SECONDS[m.group(2).lower()]
    body = (text[:m.start()] + " " + text[m.end():]).strip()
    return {"text": _strip_lead(body) or "Reminder", "due_at": now + timedelta(seconds=secs)}


async def parse_reminder(text: str, chat_service, now: Optional[datetime] = None) -> dict:
    """Parse a natural-language reminder into {ok, text, due_at, error}.

    Uses the LLM to resolve both relative ("in 10m") and absolute ("next tuesday 9am") times into
    an absolute UTC datetime, with a regex fallback for the common relative case."""
    now = now or datetime.utcnow()
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Tell me what to remind you about, e.g. `remind open the oven in 10m`."}

    parsed = None
    try:
        weekday = now.strftime("%A")
        prompt = (
            "You convert a reminder request into JSON. The current time is "
            f"{now.strftime('%Y-%m-%dT%H:%M:%S')} UTC ({weekday}).\n"
            "Return ONLY a JSON object, no prose, with exactly these keys:\n"
            '  "text": the thing to be reminded of, phrased as a short imperative WITHOUT '
            '"remind me" (e.g. "open the oven").\n'
            '  "iso": the absolute due time as "YYYY-MM-DDTHH:MM:SS" in UTC, computed from the '
            "current time for relative phrases (in 10m, in 2 hours, tomorrow, next tuesday 9am). "
            "Use null if no time is present.\n"
            f"Request: {text}"
        )
        raw = await chat_service.chat([{"role": "user", "content": prompt}])
        raw = (raw or "").strip()
        # Strip ```json fences if the model added them.
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            obj = json.loads(m.group(0))
            iso = obj.get("iso")
            body = _strip_lead(obj.get("text") or "")
            if iso:
                due = datetime.fromisoformat(str(iso).replace("Z", "").strip())
                parsed = {"text": body or text, "due_at": due}
    except Exception as e:
        logger.info(f"LLM parse failed ({e}); trying fallback")

    if not parsed:
        parsed = _fallback_parse(text, now)

    if not parsed:
        return {"ok": False, "error": (
            "I couldn't work out *when* to remind you. Try a clear time, e.g. "
            "`remind open the oven in 10m` or `remind me next tuesday to open the oven`.")}

    # Guard against times in the past (model clock drift / ambiguous parse).
    if parsed["due_at"] <= now:
        parsed["due_at"] = now + timedelta(minutes=1)
    return {"ok": True, "text": parsed["text"], "due_at": parsed["due_at"]}


# --------------------------------------------------------------------------- CRUD

def create_reminder(db: Session, user: User, text: str, due_at: datetime) -> Reminder:
    r = Reminder(user_id=user.id, text=text, due_at=due_at, status="pending")
    db.add(r)
    db.commit()
    db.refresh(r)
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
    return r


# --------------------------------------------------------------------------- formatting

def humanize_due(due_at: datetime, now: Optional[datetime] = None) -> str:
    """A short 'in 10 minutes' / 'in 2 days' phrase plus the absolute UTC time."""
    now = now or datetime.utcnow()
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
    return f"{rel} ({due_at.strftime('%Y-%m-%d %H:%M')} UTC)"


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
    db.add(Message(conversation_id=chat.id, role="assistant", content=body))
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

    # Telegram (only if configured for this user).
    if getattr(user, "telegram_enabled", False) and getattr(user, "telegram_chat_id", None):
        try:
            from app.services.telegram_service import telegram_service, configure_from_settings
            configure_from_settings(db)
            await telegram_service.send_message(user.telegram_chat_id, body)
        except Exception as e:
            logger.warning(f"telegram delivery failed for user {user.id}: {e}")


# --------------------------------------------------------------------------- scheduler

async def poll_once(db: Session) -> None:
    """Fire any reminders whose due time has passed."""
    now = datetime.utcnow()
    due = (db.query(Reminder)
           .filter(Reminder.status == "pending", Reminder.due_at <= now)
           .order_by(Reminder.due_at.asc())
           .limit(50)
           .all())
    for r in due:
        try:
            await deliver(db, r)
            r.status = "done"
            r.delivered_at = datetime.utcnow()
            db.commit()
            logger.info(f"delivered reminder #{r.id} to user {r.user_id}")
        except Exception as e:
            logger.warning(f"failed to deliver reminder #{r.id}: {e}")
            db.rollback()


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
    _scheduler.add_job(_job, "interval", seconds=30, id="reminder_poll", max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("reminder poller started (every 30s)")


def stop_reminder_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
        logger.info("reminder poller stopped")
