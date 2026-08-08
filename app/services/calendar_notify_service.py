"""Calendar alarms → reminders, so an appointment reaches a phone that is not open.

The calendar already stores what people want to be told about: 205 of one real 707-event
calendar's items carry a `VALARM`, which is a client saying "warn me 15 minutes before". Nothing
was acting on them — the month grid drew the event and that was the end of it.

Rather than build a second delivery path, this turns a due alarm into a **Reminder** row. Reminders
already fire exactly once (an atomic pending→done claim), and already deliver to the web UI, to web
push (so a closed phone buzzes) and to Telegram when it is linked. One notification pipeline, not
two that drift.

WHY THE EXPANSION IS `dateutil` AND NOT A PORT OF ical.js. Recurrence lives in the client
(static/js/client/ical.js) because that is where the month grid needs it, and a phone needs this on
the SERVER. Hand-porting those rules would be two implementations of the hardest part of iCalendar,
guaranteed to disagree eventually. `dateutil.rrule` is a mature RFC-5545 implementation that is
already a pinned dependency, so the server uses that instead of a copy of our own logic.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from app.models import Reminder, User

logger = logging.getLogger(__name__)

# How far ahead alarms are scheduled, and how often that is worked out. These two numbers are a pair,
# and the interval is HOURLY on purpose.
#
# A pass reads every event this user has and NIP-44-decrypts each one — 707 documents for one real
# imported calendar. Running that every few minutes is precisely the shape of the bug that pegged the
# event loop in the mail sync ("a decrypt of the whole folder on every pass"), and it would buy
# nothing: because the horizon is 26 hours, an alarm is turned into a Reminder row long before it is
# due, and the 15-second reminder poller is what actually delivers it. One expensive read an hour
# feeds a cheap poller that runs all the time.
_HORIZON = timedelta(hours=26)          # far enough to cover an all-day event's morning alarm
_POLL_SECONDS = 3600

# Users with no calendar at all are the common case on a multi-user node, and re-reading their
# (empty) calendar list every pass is the bulk of the cost. Remember them and look again occasionally,
# so turning the calendar on is noticed within the hour without paying for it every time.
_NO_CALENDARS: dict = {}
_RECHECK_AFTER = 6 * 3600

# A brand-new calendar (or a fresh import of ten years of history) must not deliver a single
# notification about the past. Only alarms in the FUTURE are ever scheduled.
_MIN_LEAD = timedelta(seconds=30)


def _unfold(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n ", "").replace("\n\t", "")


def _prop(component: str, name: str):
    """(value, params) for the first `name` property, or (None, {})."""
    for line in _unfold(component).split("\n"):
        head, _, value = line.partition(":")
        bits = head.split(";")
        if bits[0].upper() != name.upper():
            continue
        params = {}
        for p in bits[1:]:
            k, _, v = p.partition("=")
            params[k.upper()] = v.strip('"')
        return value.strip(), params
    return None, {}


def _parse_dt(value: str, params: dict):
    """An iCalendar date/date-time → an aware UTC datetime, or None.

    An all-day value is a DATE with no time; it is anchored at local midnight of the SERVER, which is
    the best available answer for a node whose users share its timezone and is why the alarm for one
    is a lead time rather than an exact instant.
    """
    v = (value or "").strip()
    m = re.match(r"^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?$", v)
    if not m:
        return None
    y, mo, d, hh, mi, ss, z = m.groups()
    base = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0))
    if z:
        return base.replace(tzinfo=timezone.utc)
    tzid = (params or {}).get("TZID")
    if tzid:
        try:
            from zoneinfo import ZoneInfo
            return base.replace(tzinfo=ZoneInfo(tzid)).astimezone(timezone.utc)
        except Exception:
            pass          # not an IANA zone (airline exports write things like GMT-0600)
    return base.astimezone(timezone.utc)      # floating: the server's own clock


def _triggers(component: str) -> list:
    """Lead times from every VALARM in a component, as positive timedeltas before the start.

    An absolute TRIGGER (`VALUE=DATE-TIME`) is ignored rather than guessed at: it is rare, and
    treating an absolute instant as an offset would fire an alarm at a wildly wrong time.
    """
    out = []
    for block in re.findall(r"(?is)BEGIN:VALARM(.*?)END:VALARM", _unfold(component)):
        for line in block.split("\n"):
            head, _, value = line.partition(":")
            bits = head.split(";")
            if bits[0].strip().upper() != "TRIGGER":
                continue
            params = {}
            for p in bits[1:]:
                k, _, v = p.partition("=")
                params[k.upper()] = v.strip('"')
            if str(params.get("VALUE", "")).upper() == "DATE-TIME":
                continue
            m = re.match(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$",
                         value.strip().upper())
            if not m:
                continue
            sign, w, d, h, mi, s = m.groups()
            delta = timedelta(weeks=int(w or 0), days=int(d or 0), hours=int(h or 0),
                              minutes=int(mi or 0), seconds=int(s or 0))
            # A negative duration means "before the start", which is what a lead time is.
            out.append(delta if sign == "-" else -delta)
    return [t for t in out if t is not None]


def _occurrences(comp: str, start, frm, to) -> list:
    """Every start time of this component within [frm, to), recurrence included."""
    rrule_val, _ = _prop(comp, "RRULE")
    if not rrule_val:
        return [start] if frm <= start < to else []
    try:
        from dateutil.rrule import rrulestr
        rule = rrulestr(f"RRULE:{rrule_val}", dtstart=start)
        out = list(rule.between(frm, to, inc=True))
    except Exception as e:
        logger.debug("[cal-notify] unreadable RRULE %r: %s", rrule_val, e)
        return [start] if frm <= start < to else []
    # EXDATEs cancel individual occurrences.
    ex = set()
    for line in _unfold(comp).split("\n"):
        head, _, value = line.partition(":")
        bits = head.split(";")
        if bits[0].upper() != "EXDATE":
            continue
        params = {}
        for p in bits[1:]:
            k, _, v = p.partition("=")
            params[k.upper()] = v.strip('"')
        for one in value.split(","):
            d = _parse_dt(one.strip(), params)
            if d:
                ex.add(d.replace(second=0, microsecond=0))
    return [o for o in out if o.replace(second=0, microsecond=0) not in ex]


async def due_alarms(db, user, now=None) -> list:
    """[(when_utc, title)] for every alarm this user should be told about before the horizon."""
    from app.services import caldav_store
    now = now or datetime.now(timezone.utc)
    horizon = now + _HORIZON
    out = []
    import time as _time
    skip_until = _NO_CALENDARS.get(user.id)
    if skip_until and _time.time() < skip_until:
        return []
    try:
        cals = await caldav_store.list_calendars(db, user, strict=True)
    except Exception as e:
        # An unreachable relay is not an empty calendar. Skip the pass rather than deliver nothing
        # and, worse, than deliver a duplicate on the next one.
        logger.debug("[cal-notify] calendars unreadable for %s: %s", user.username, e)
        return []
    if not cals:
        # Remembered, not concluded: re-checked in a few hours so switching the calendar on is
        # noticed without every pass paying to ask everyone who has never used it.
        _NO_CALENDARS[user.id] = _time.time() + _RECHECK_AFTER
        return []
    _NO_CALENDARS.pop(user.id, None)
    for cal in cals:
        cid = cal.get("id")
        if not cid:
            continue
        try:
            items = await caldav_store.get_items(db, user, cid, strict=True)
        except Exception:
            continue
        for rec in items:
            ics = rec.get("ics") or ""
            for comp in caldav_store.split_ics(ics):
                kind = caldav_store.component_of(comp)
                if kind not in ("VEVENT", "VTODO"):
                    continue
                dtv, dtp = _prop(comp, "DTSTART" if kind == "VEVENT" else "DUE")
                if not dtv:
                    continue
                start = _parse_dt(dtv, dtp)
                if not start:
                    continue
                leads = _triggers(comp)
                if not leads:
                    continue      # no VALARM → the user never asked to be told about this one
                title = (_prop(comp, "SUMMARY")[0] or "(no title)").replace("\\,", ",") \
                    .replace("\\;", ";").replace("\\n", " ").strip()
                for occ in _occurrences(comp, start, now - _HORIZON, horizon):
                    for lead in leads:
                        when = occ - lead
                        if when < now + _MIN_LEAD or when >= horizon:
                            continue
                        mins = int(lead.total_seconds() // 60)
                        when_txt = ("now" if mins <= 0 else
                                    f"in {mins} minutes" if mins < 60 else
                                    f"in {mins // 60}h" if mins % 60 == 0 else
                                    f"in {mins // 60}h{mins % 60:02d}")
                        out.append((when.replace(tzinfo=None), f"📅 {title} — {when_txt}"))
    return out


async def poll_once(db) -> int:
    """Schedule reminders for alarms coming due. Returns how many were created."""
    from app.services import caldav_store
    if not caldav_store.enabled():
        return 0
    made = 0
    # Every account, because there is no cheap way to ask "does this user have a calendar?" without
    # reading their documents — each user's are encrypted under their OWN key, so there is no single
    # query across them. The cost is bounded instead by _NO_CALENDARS: the first pass pays for one
    # (small) metadata scan per account and every pass after that skips the ones with nothing, for
    # hours at a time.
    users = db.query(User).all()
    for user in users:
        try:
            alarms = await due_alarms(db, user)
        except Exception as e:
            logger.debug("[cal-notify] %s: %s", getattr(user, "username", "?"), e)
            continue
        for when, text in alarms:
            # Dedup on (user, text, due_at): the poller runs every few minutes and sees the same
            # alarm each time until it fires. Without this, an event an hour away collects a dozen
            # identical reminders and the phone buzzes a dozen times.
            exists = (db.query(Reminder.id)
                      .filter(Reminder.user_id == user.id, Reminder.text == text,
                              Reminder.due_at == when)
                      .first())
            if exists:
                continue
            db.add(Reminder(user_id=user.id, text=text, due_at=when, status="pending"))
            made += 1
        if made:
            db.commit()
    if made:
        logger.info("[cal-notify] scheduled %s calendar reminder(s)", made)
    return made


_scheduler = None


def start_calendar_notify_scheduler() -> None:
    """Idempotent. Runs beside the reminder poller on the port-3051 instance — it only WRITES
    reminder rows; the reminder poller is what delivers them."""
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
            logger.warning("[cal-notify] poll error: %s", e)
        finally:
            _db.close()

    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_job, "interval", seconds=_POLL_SECONDS, id="calendar_notify",
                       max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("[cal-notify] calendar alarm poller started (every %ss)", _POLL_SECONDS)


def stop_calendar_notify_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
