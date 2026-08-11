"""Subscribed calendars — a read-only mirror of somebody else's published .ics.

A school term, a sports fixture list, a national holiday feed: a URL that publishes iCalendar and is
re-fetched now and then. It is NOT the same thing as an import, and the difference is the whole
design:

  * an IMPORT is a one-off copy that then belongs to you and is yours to edit;
  * a SUBSCRIPTION is a MIRROR. The remote end is the truth, so a refresh must also DELETE what the
    feed has dropped — a cancelled match that lingers forever is worse than not subscribing — and
    editing an event locally is pointless, because the next refresh overwrites it.

WHERE THE SUBSCRIPTION LIVES. On the calendar's own metadata document (`pcai:calmeta:<id>`), as
`subscribe: {url, refreshed, etag, error}`. No new table and no second source of truth: a subscribed
calendar IS a calendar, so it lists, exports, syncs to a phone over CalDAV and is encrypted exactly
like the ones you type into yourself. Un-subscribing is deleting one key, and what is left is an
ordinary calendar holding the last copy of the feed — which is the right thing to be left with.

FETCHING A USER-SUPPLIED URL FROM THE SERVER IS SSRF, and that is the sharp edge here. Every fetch
goes through the same guard the fediverse bridge and the web-search page fetcher use, and — the part
that is easy to miss and was a real hole in `fetch_url_content` — the guard is re-checked on EVERY
REDIRECT HOP, because a public URL that 302s to 169.254.169.254 passes a check made only on the first
one.
"""
from __future__ import annotations

import logging
import re
import time

import httpx
from starlette.concurrency import run_in_threadpool

from app.services import caldav_store, rss_service

logger = logging.getLogger(__name__)

# A calendar feed is text. A school district's year is a few hundred KB; anything past this is either
# not a calendar or is not something a single-worker node should be parsing on a timer.
MAX_BYTES = 8_000_000
MAX_ITEMS = 5000
MAX_REDIRECTS = 4
TIMEOUT = 25.0

# How stale a subscription may get before a refresh is due. Feeds like these change on human
# timescales — a school publishes next term, not next minute — and every refresh is a full re-import
# of every event, so this is deliberately not eager.
DEFAULT_EVERY_HOURS = 6


def normalize_url(url: str) -> str:
    """`webcal://` is what every calendar publisher links, and it is just https with a scheme nobody
    implements. Rewriting it here means a person can paste the link they were actually given."""
    u = (url or "").strip()
    if u.lower().startswith("webcal://"):
        u = "https://" + u[9:]
    elif u.lower().startswith("webcals://"):
        u = "https://" + u[10:]
    return u


def subscription_of(meta: dict) -> dict | None:
    sub = (meta or {}).get("subscribe")
    return sub if isinstance(sub, dict) and sub.get("url") else None


class CertificateProblem(ValueError):
    """The feed's TLS certificate would not verify.

    Its own class because it is the ONE failure with a sensible second answer. Everything else here
    (a 404, a web page instead of a feed, a private address) means "you cannot have this"; a
    certificate that will not chain usually means the publisher's server is misconfigured or is using
    a root the trust stores have not shipped yet — which is not the reader's fault and not something
    they can fix, and refusing outright makes the feature look broken.

    MEASURED, on the feed this was built for: canoncityschools.org serves a chain ending at "ISRG
    Root YR", a Let's Encrypt root that neither certifi 2026.05 nor this OS carries. Perfectly valid
    certificate, nothing wrong with the site, and every correct client refuses it.
    """


def _is_cert_error(e: Exception) -> bool:
    txt = str(e).upper()
    return ("CERTIFICATE_VERIFY_FAILED" in txt or "SSL" in type(e).__name__.upper()
            or "CERTIFICATE" in txt)


async def fetch_ics(url: str, etag: str = "", insecure: bool = False) -> tuple[str, str, bool]:
    """Fetch a calendar feed. Returns (text, etag, changed).

    Redirects are followed BY HAND so the SSRF guard runs on every hop — httpx's own
    `follow_redirects` would validate the first URL and then happily fetch wherever it is sent, which
    is the exact hole that once let a redirect reach a cloud metadata endpoint.

    `insecure` skips CERTIFICATE verification only, and only because the reader asked for it on this
    one feed (see CertificateProblem). It does NOT relax the SSRF guard, which is the check that
    actually protects this server — the two are unrelated, and conflating them would turn a cosmetic
    trust-store gap into a way to read the metadata endpoint.
    """
    cur = normalize_url(url)
    headers = {"User-Agent": "PosterChanAI/1.0 (calendar subscription)",
               "Accept": "text/calendar, text/plain;q=0.8, */*;q=0.5"}
    if etag:
        # A conditional request turns "nothing changed" into 304 and a few hundred bytes, which is
        # most refreshes of most feeds.
        headers["If-None-Match"] = etag

    async with httpx.AsyncClient(follow_redirects=False, timeout=TIMEOUT,
                                 verify=not insecure) as client:
        for _ in range(MAX_REDIRECTS + 1):
            if not rss_service.looks_fetchable(cur):
                raise ValueError("that address cannot be fetched (it must be a public http/https URL)")
            if not await run_in_threadpool(rss_service.is_safe_host, cur):
                raise ValueError("that address resolves to a private network and will not be fetched")
            try:
                r = await client.get(cur, headers=headers)
            except Exception as e:
                if not insecure and _is_cert_error(e):
                    raise CertificateProblem(
                        "that site's security certificate could not be verified — usually its own "
                        "misconfiguration, or a certificate authority this server does not know yet"
                    ) from e
                raise
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location") or ""
                if not loc:
                    raise ValueError("the server redirected without saying where")
                cur = str(httpx.URL(cur).join(loc))
                continue
            if r.status_code == 304:
                return "", etag, False
            if r.status_code >= 400:
                raise ValueError(f"the calendar server answered {r.status_code}")
            # Length first, then the body: a Content-Length we can trust saves downloading a file we
            # are going to refuse anyway.
            n = int(r.headers.get("content-length") or 0)
            if n and n > MAX_BYTES:
                raise ValueError("that calendar is too large to subscribe to")
            raw = r.content
            if len(raw) > MAX_BYTES:
                raise ValueError("that calendar is too large to subscribe to")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("latin-1", errors="replace")
            if "BEGIN:VCALENDAR" not in text.upper():
                # Naming what it looks like beats "invalid": the single most common mistake is
                # pasting the page the calendar is ON rather than the feed it links to.
                raise ValueError("that URL did not return a calendar — it looks like a web page. "
                                 "Look for a 'subscribe' or '.ics' link on it.")
            return text, (r.headers.get("etag") or ""), True
    raise ValueError("too many redirects")


async def refresh(db, user, cal_id: str, meta: dict) -> dict:
    """Re-mirror one subscribed calendar. Returns a status dict; never raises."""
    sub = subscription_of(meta)
    if not sub:
        return {"ok": False, "error": "not a subscription"}
    out = dict(sub)
    out["checked"] = int(time.time())
    try:
        text, etag, changed = await fetch_ics(sub["url"], str(sub.get("etag") or ""),
                                              insecure=bool(sub.get("insecure")))
    except Exception as e:
        # The ERROR IS STORED, so the UI can say why a calendar has stopped updating instead of
        # showing stale events that look current. A failed refresh never deletes anything.
        out["error"] = str(e)[:200]
        await _save_meta(db, user, cal_id, meta, out)
        logger.info("[calsub] %s refresh failed: %s", cal_id, e)
        return {"ok": False, "error": out["error"]}

    if not changed:
        out["error"] = ""
        out["refreshed"] = int(time.time())
        await _save_meta(db, user, cal_id, meta, out)
        return {"ok": True, "unchanged": True, "added": 0, "removed": 0}

    resources = caldav_store.group_resources(text)
    if len(resources) > MAX_ITEMS:
        out["error"] = f"that feed holds {len(resources)} items, more than this app subscribes to"
        await _save_meta(db, user, cal_id, meta, out)
        return {"ok": False, "error": out["error"]}
    tzs = caldav_store.timezones_of(text)

    # WHAT IS HERE NOW, so the mirror can drop what the feed has. Read BEFORE writing, and a failure
    # to read is a refusal to prune — deleting on the strength of an empty read is the replaceable-doc
    # wipe in a different costume, and it would empty somebody's calendar.
    have, prunable = {}, True
    try:
        for it in await caldav_store.get_items(db, user, cal_id):
            uid = it.get("uid") or ""
            if uid:
                have[uid] = it
    except Exception as e:
        logger.warning("[calsub] %s: could not read current items, not pruning: %s", cal_id, e)
        prunable = False

    import asyncio
    sem = asyncio.Semaphore(8)
    seen = set()

    async def _one(res):
        uid, comp, parts = res
        seen.add(uid)
        body = caldav_store.wrap_ics(parts, cal_id, timezones=tzs)
        async with sem:
            return await caldav_store.put_item(db, user, cal_id, uid, body, comp)

    results = await asyncio.gather(*[_one(r) for r in resources], return_exceptions=True)
    added = sum(1 for r in results if r is True)

    removed = 0
    if prunable:
        gone = [u for u in have if u not in seen]
        for uid in gone:
            try:
                if await caldav_store.delete_item(db, user, cal_id, uid):
                    removed += 1
            except Exception:
                pass

    out["error"] = ""
    out["etag"] = etag
    out["refreshed"] = int(time.time())
    out["count"] = len(resources)
    await _save_meta(db, user, cal_id, meta, out)
    logger.info("[calsub] %s: %d in feed, %d written, %d removed", cal_id, len(resources), added, removed)
    return {"ok": True, "added": added, "removed": removed, "count": len(resources)}


async def _save_meta(db, user, cal_id: str, meta: dict, sub: dict) -> None:
    # `id` is SYNTHETIC — list_calendars adds it from the document's d-tag. Writing it back into the
    # document makes the doc claim an id it does not own, which is one rename away from a calendar
    # whose stored id and real id disagree.
    m = {k: v for k, v in (meta or {}).items() if k != "id"}
    m["subscribe"] = sub
    try:
        await caldav_store.put_calendar(db, user, cal_id, m)
    except Exception as e:
        logger.warning("[calsub] could not store %s's subscription state: %s", cal_id, e)


def due(sub: dict, every_hours: float = DEFAULT_EVERY_HOURS) -> bool:
    """Whether this subscription is old enough to re-fetch.

    A FAILING one is retried on the same schedule rather than backing off: these feeds fail because a
    school's web host is down for an afternoon, not because the URL is wrong, and a subscription that
    quietly gives up is the thing nobody notices until they miss something.
    """
    last = 0
    try:
        last = int(sub.get("checked") or sub.get("refreshed") or 0)
    except Exception:
        last = 0
    return (time.time() - last) >= max(900.0, float(every_hours) * 3600.0)


_ID_SAFE = re.compile(r"[^a-z0-9_-]+")


def id_for(url: str, title: str = "") -> str:
    """A stable, readable id for a new subscription — the feed's own name when it has one, else its
    host. Only a SUGGESTION: the caller resolves collisions with caldav_store.free_id."""
    base = _ID_SAFE.sub("-", (title or "").strip().lower()).strip("-")
    if not base:
        base = _ID_SAFE.sub("-", host_label(url)).strip("-")
    return (base or "feed")[:40]


def host_label(url: str) -> str:
    """A readable name from the URL, for a feed that publishes no X-WR-CALNAME — which the one this
    was built for does not. `www.canoncityschools.org` → `canoncityschools`, because
    "www-canoncityschools-org" as the name of a calendar in a sidebar is an id leaking into a label."""
    from urllib.parse import urlparse
    host = (urlparse(normalize_url(url)).hostname or "").lower()
    parts = [p for p in host.split(".") if p and p != "www"]
    # Drop the public suffix, but never the whole thing: `example.co.uk` keeps `example`.
    while len(parts) > 1 and len(parts[-1]) <= 3:
        parts.pop()
    return (parts[-1] if parts else "calendar")[:40]


def name_in(text: str) -> str:
    """The feed's own display name (`X-WR-CALNAME`), which is what the publisher wants it called."""
    m = re.search(r"^X-WR-CALNAME[;:]([^\r\n]*)", text or "", re.M | re.I)
    if not m:
        return ""
    val = m.group(1)
    if ":" in val and "=" in val.split(":")[0]:      # parameters before the value
        val = val.split(":", 1)[1]
    return val.strip()[:80]


# ---------------------------------------------------------------------------------------------
# the background refresh (worker)
#
# WHY IT IS NOT ENOUGH TO REFRESH WHEN THE APP OPENS. These calendars are read on a PHONE, over
# CalDAV, by the phone's own calendar app — which never opens PosterChan. A subscription that only
# updated when somebody looked at the web UI would show a school's new term on the laptop and last
# term's on the phone, which is worse than not having the feature: the phone is where people actually
# look, and it would be confidently wrong.

_scheduler = None
_TICK = 30 * 60          # look for DUE subscriptions this often; `due()` decides what is actually
                         # fetched, so this is a scan, not a fetch.

# Users with NO subscriptions, and when we last checked. Listing a user's calendars is a relay query,
# and doing that for every account on the node every half hour — for a feature almost nobody on it
# uses — is the kind of steady background cost that only shows up as "why is the relay busy". Anyone
# who has never had a subscription is re-checked every few hours instead; a new one made through the
# API is fetched by the endpoint itself, so nothing waits on this.
_QUIET: dict = {}
_QUIET_EVERY = 6 * 3600


async def _tick() -> None:
    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        if not caldav_store.enabled():
            return
        try:
            users = db.query(User).all()
        except Exception as e:
            logger.warning("[calsub] could not list users: %s", e)
            return
        now = time.time()
        for user in users:
            uid = getattr(user, "id", None)
            if uid is not None and (now - _QUIET.get(uid, 0)) < _QUIET_EVERY:
                continue
            try:
                cals = await caldav_store.list_calendars(db, user)
            except Exception:
                # An unreadable calendar list is a relay blip, not "this user has no calendars" —
                # and the difference matters because refresh() PRUNES. Skip the user this tick.
                continue
            subs = [c for c in cals if subscription_of(c)]
            if uid is not None:
                # Remember the answer either way: a user WITH subscriptions is checked every tick
                # (the per-feed `due` is what paces the fetching), one without is left alone.
                if subs:
                    _QUIET.pop(uid, None)
                else:
                    _QUIET[uid] = now
            for c in subs:
                sub = subscription_of(c)
                if not due(sub):
                    continue
                try:
                    res = await refresh(db, user, c.get("id"), c)
                    if res.get("ok") and not res.get("unchanged"):
                        # A phone reads through Radicale's cache; without this the events are on the
                        # relay and invisible until the app restarts.
                        try:
                            from app.services.caldav import storage as _cst
                            _cst.forget_user(getattr(user, "username", ""))
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning("[calsub] %s/%s failed: %s",
                                   getattr(user, "username", "?"), c.get("id"), e)
    finally:
        db.close()


def start_calendar_subscriptions_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    _scheduler = AsyncIOScheduler()
    _scheduler.add_job(_tick, IntervalTrigger(seconds=_TICK), id="calendar-subscriptions",
                       name="Subscribed calendars", replace_existing=True,
                       coalesce=True, max_instances=1, misfire_grace_time=_TICK)
    _scheduler.start()
    logger.info("[calsub] scheduler started (scan every %ds)", _TICK)


def stop_calendar_subscriptions_scheduler():
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown()
        except Exception:
            pass
        _scheduler = None
        logger.info("[calsub] scheduler stopped")
