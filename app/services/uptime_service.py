"""Uptime monitoring — the Uptime-Kuma-shaped half of Discover → Server Stats.

An admin lists endpoints in Admin → Services ("Uptime Monitoring"); this poller checks each one on
its own interval, keeps a heartbeat history, and alerts on a state CHANGE (up→down, down→up) over
Telegram and/or a Nostr DM.

Where the state lives: in the relay, as ONE operator-signed kind-30078 doc (`pcai:kv:uptime`), not a
new SQL table — same rule the rest of this codebase follows for new-feature state. That also solves
the process split for free: the checks run in the WORKER process (like every other poller), while
`/client/uptime` is served from the APP process, and a replaceable relay doc is the shared store both
already know how to reach. kind 30078 is parameterized-replaceable, so re-putting `pcai:kv:uptime`
replaces it in place — the history never accumulates events, it accumulates INSIDE one document.

Cost discipline, in the same spirit as stats_service:
  * checks run concurrently but bounded (_MAX_CONCURRENCY), so 40 monitors don't open 40 sockets;
  * the doc is written at most once per _PERSIST_SECONDS **unless** a status flipped, in which case
    it's written immediately (a status page that lags a real outage by a minute is the one moment it
    had a job to do);
  * the read side (`get_status`) caches the doc for _READ_TTL seconds, so a page full of viewers
    costs one relay query per TTL, not one per viewer.

History is bounded per monitor: the last _KEEP_CHECKS heartbeats (the Kuma-style bar), 48 HOURLY
[ok, total] aggregates and 30 DAILY ones. The two aggregate levels exist so the percentages mean what
they say — a rolling 24h needs hour buckets (a single day bucket would make "24h" silently mean
"since UTC midnight"), while 30 days would be wasteful at hourly resolution. All three are trimmed on
every write, so the document can't grow without limit.
"""

import re
import time
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

uptime_scheduler = None

DOC = "pcai:kv:uptime"          # the one relay doc holding every monitor's state + history
_TICK = 20                      # base tick; each monitor is checked when ITS interval is due
_KEEP_CHECKS = 120              # heartbeats kept per monitor (the bar on the page)
_KEEP_HOURS = 48                # hourly aggregates kept per monitor (the rolling 24h figure)
_KEEP_DAYS = 30                 # daily aggregates kept per monitor
_PERSIST_SECONDS = 60           # max age of the persisted doc while nothing changes
_MAX_CONCURRENCY = 8
_READ_TTL = 15.0                # seconds the app process serves one snapshot of the doc
_BODY_LIMIT = 256 * 1024        # bytes read when an expected-text check needs the body (see check_one)

_state: dict = {}               # {monitor_id: record} — the worker's live copy
_loaded = False
_last_persist = 0.0
_read_cache = {"at": 0.0, "data": None}


# ---- monitor definitions ------------------------------------------------

def _slug(name: str) -> str:
    """Stable id for a monitor. It keys the history, so it is derived from the NAME only: editing a
    monitor's URL keeps its history, renaming it starts a fresh one (which is the honest behaviour —
    a renamed monitor is usually a different thing)."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or "monitor"


def parse_monitors() -> list:
    """Parse the `uptime_monitors` setting into monitor dicts.

    One per line:  `Name | https://example.com | interval_seconds | expected text`
    Only the URL is required — a bare URL line takes its name from the host, the interval falls back
    to `uptime_interval_seconds`, and without an expected-text field any 2xx/3xx counts as up.
    """
    from app.services import settings_store
    raw = settings_store.get("uptime_monitors", "") or ""
    default_interval = max(20, settings_store.get_int("uptime_interval_seconds", 60))
    out, seen = [], set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        # A bare URL line is the common case when pasting a list — accept it.
        if len(parts) == 1:
            url, name = parts[0], ""
        else:
            name, url = parts[0], parts[1]
        if not url.lower().startswith(("http://", "https://")):
            continue
        if not name:
            name = re.sub(r"^https?://", "", url).split("/")[0]
        interval = default_interval
        if len(parts) >= 3 and parts[2]:
            try:
                interval = max(20, int(parts[2]))
            except ValueError:
                pass
        keyword = parts[3] if len(parts) >= 4 else ""
        mid = _slug(name)
        while mid in seen:                       # two monitors named the same must not share history
            mid += "-2"
        seen.add(mid)
        out.append({"id": mid, "name": name, "url": url, "interval": interval, "keyword": keyword})
    return out


# ---- persistence (relay doc) --------------------------------------------

def _relay_port() -> int:
    from app.services import settings_store
    return settings_store.get_int("nostr_relay_port", 3052)


async def _load() -> None:
    """Hydrate `_state` from the relay doc once per process, so a worker restart doesn't wipe the
    heartbeat history (or re-alert for an outage it already reported)."""
    global _loaded
    if _loaded:
        return
    try:
        from app.database import SessionLocal
        from app.services import settings_store, nostr_store
        db = SessionLocal()
        try:
            sk = settings_store._operator_seckey(db)
        finally:
            db.close()
        if not sk:
            return                                # no operator key yet — retry on the next tick
        # strict: an unreachable relay must RAISE, not look like "no document yet" — see
        # nostr_store._ws_query. Without it a failed read would set _loaded and let the
        # next persist replace the whole history with an empty one.
        doc = await nostr_store.get_doc(_relay_port(), DOC, seckey=sk, strict=True)
        mons = (doc or {}).get("monitors") if isinstance(doc, dict) else None
        if isinstance(mons, dict):
            _state.update({k: v for k, v in mons.items() if isinstance(v, dict)})
            logger.info("[uptime] restored %d monitors from the relay", len(_state))
        # Only NOW is the restore settled. Marking it loaded up-front would mean a relay that wasn't
        # ready on the first tick permanently lost the history — and with it the up/down state, so
        # every monitor would re-alert as if it had just changed.
        _loaded = True
    except Exception as e:
        logger.warning("[uptime] could not restore state (will retry): %s", e)


async def _persist(force: bool = False) -> None:
    global _last_persist
    if not _loaded:
        # NEVER write state we failed to READ. The doc is replaceable, so persisting an empty `_state`
        # after a failed restore would replace the whole history — the same way a browser holding an
        # empty default wiped a drive's folder index. No read, no write.
        return
    now = time.monotonic()
    if not force and (now - _last_persist) < _PERSIST_SECONDS:
        return
    _last_persist = now
    try:
        from app.database import SessionLocal
        from app.services import settings_store, nostr_store
        db = SessionLocal()
        try:
            sk = settings_store._operator_seckey(db)
        finally:
            db.close()
        if not sk:
            return
        await nostr_store.put_doc(_relay_port(), sk, DOC,
                                  {"updated": int(time.time()), "monitors": _state})
    except Exception as e:
        logger.warning("[uptime] persist failed: %s", e)


# ---- checking -----------------------------------------------------------

async def check_one(mon: dict, timeout: float) -> dict:
    """Perform ONE check. Returns {ok, ms, code, err} and never raises: a monitor that blows up in an
    unexpected way is a DOWN monitor, not a dead poller."""
    import httpx
    t0 = time.monotonic()
    try:
        # STREAM and stop early. A plain .get() downloads the whole body every interval — a 5 MB page
        # polled each minute is ~7 GB/day of pointless traffic for a yes/no answer. We only need the
        # status line, plus enough body to find the expected text when one is configured.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", mon["url"],
                                     headers={"User-Agent": "PosterChanAI-Uptime/1.0"}) as r:
                code = r.status_code
                body = b""
                kw = mon.get("keyword") or ""
                if kw and code < 400:
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) >= _BODY_LIMIT:
                            break
        ms = int((time.monotonic() - t0) * 1000)
        if code >= 400:
            return {"ok": False, "ms": ms, "code": code, "err": f"HTTP {code}"}
        if kw:
            text = body[:_BODY_LIMIT].decode("utf-8", "replace")
            if kw not in text:
                return {"ok": False, "ms": ms, "code": code, "err": f"missing text: {kw[:40]}"}
        return {"ok": True, "ms": ms, "code": code, "err": ""}
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        return {"ok": False, "ms": ms, "code": 0, "err": (str(e) or e.__class__.__name__)[:160]}


def _record(mon: dict, res: dict, retries: int) -> tuple:
    """Fold one result into the monitor's record. Returns (record, transition) where transition is
    'down', 'up' or None — the alerting is driven off that, so a flapping check that never crosses
    the `retries` threshold never pages anyone."""
    now = int(time.time())
    rec = _state.get(mon["id"]) or {}
    prev = rec.get("status", "pending")
    fails = int(rec.get("fails", 0))
    fails = 0 if res["ok"] else fails + 1

    if res["ok"]:
        status = "up"
    elif fails >= max(1, retries):
        status = "down"
    else:
        # Not enough consecutive failures yet — stay in the old state (a single blip is not an
        # outage), but say so on the page rather than pretending the check passed.
        status = prev if prev in ("up", "down") else "pending"

    transition = None
    if status != prev and status in ("up", "down") and prev != "pending":
        transition = status
    elif prev == "pending" and status == "down":
        transition = "down"        # first observation is already down: worth saying once

    checks = list(rec.get("checks") or [])
    checks.append([now, 1 if res["ok"] else 0, int(res["ms"])])
    checks = checks[-_KEEP_CHECKS:]

    def _bucket(store, key, keep, with_ms):
        b = dict(store or {})
        cur = b.get(key) or ([0, 0, 0] if with_ms else [0, 0])
        cur = list(cur) + [0, 0, 0]
        b[key] = ([int(cur[0]) + (1 if res["ok"] else 0), int(cur[1]) + 1, int(cur[2]) + int(res["ms"])]
                  if with_ms else [int(cur[0]) + (1 if res["ok"] else 0), int(cur[1]) + 1])
        return dict(sorted(b.items())[-keep:])     # keys are sortable timestamps → oldest fall off

    hourly = _bucket(rec.get("hourly"), time.strftime("%Y-%m-%dT%H", time.gmtime(now)), _KEEP_HOURS, False)
    daily = _bucket(rec.get("daily"), time.strftime("%Y-%m-%d", time.gmtime(now)), _KEEP_DAYS, True)

    out = {
        "id": mon["id"], "name": mon["name"], "url": mon["url"], "interval": mon["interval"],
        "status": status, "fails": fails, "last": now, "ms": int(res["ms"]),
        "code": int(res.get("code") or 0), "err": res.get("err") or "",
        "since": now if status != prev else int(rec.get("since") or now),
        "checks": checks, "hourly": hourly, "daily": daily,
    }
    return out, transition


# ---- alerting -----------------------------------------------------------

def _alert_text(rec: dict, kind: str) -> str:
    from app.services import settings_store
    site = (settings_store.get("site_name", "") or "this server").strip()
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(rec.get("last") or time.time()))
    if kind == "down":
        return (f"🔴 DOWN — {rec['name']}\n{rec['url']}\n"
                f"{rec.get('err') or 'no response'}\n{when} · {site}")
    return (f"🟢 UP — {rec['name']}\n{rec['url']}\n"
            f"responded in {rec.get('ms', 0)} ms\n{when} · {site}")


async def _alert(rec: dict, kind: str) -> None:
    """Fan a transition out to the configured channels. Best-effort: an alert that can't be
    delivered must never stop the poller (or the other channel) — every path is caught."""
    from app.services import settings_store
    text = _alert_text(rec, kind)

    if settings_store.get_bool("uptime_alert_telegram", False):
        try:
            from app.database import SessionLocal
            from app.models import User
            db = SessionLocal()
            try:
                admin = db.query(User).filter(User.id == 1).first()
                chat_id = getattr(admin, "telegram_chat_id", None) if admin else None
                enabled = bool(admin and admin.telegram_enabled and chat_id)
                if enabled:
                    from app.services.telegram_service import telegram_service, configure_from_settings
                    token = settings_store.get("telegram_bot_token", "")
                    if token:
                        telegram_service.set_token(token)
                    configure_from_settings(db)
                    # parse_mode="" — NOT the Markdown default. The text carries a raw URL and an
                    # arbitrary error string ("[Errno 111]", a path with underscores), any of which
                    # Telegram rejects as unparseable entities. There is a retry-without-formatting
                    # fallback in send_message, but a DOWN alert is the last message that should be
                    # spending a round trip discovering that.
                    await telegram_service.send_message(chat_id, text, parse_mode="")
            finally:
                db.close()
        except Exception as e:
            logger.warning("[uptime] telegram alert failed: %s", e)

    if settings_store.get_bool("uptime_alert_nostr", False):
        try:
            await _alert_nostr(text)
        except Exception as e:
            logger.warning("[uptime] nostr alert failed: %s", e)


async def _alert_nostr(text: str) -> None:
    """NIP-17 gift-wrapped DM from the instance's OPERATOR key to each configured npub (default: the
    admin's own npub), published to the LOCAL relay only — it federates outward from there, which is
    the rule every publisher in this codebase follows."""
    from app.database import SessionLocal
    from app.models import User
    from app.services import settings_store, keystore
    from app.services.nostr import nostr_service, nip17
    from app.services.nostr_store import publish_event

    targets = [x.strip() for x in
               (settings_store.get("uptime_alert_npubs", "") or "").replace(",", "\n").splitlines()
               if x.strip()]
    if not targets:
        db = SessionLocal()
        try:
            admin = db.query(User).filter(User.id == 1).first()
            npub = (getattr(admin, "nostr_npub", "") or "").strip() if admin else ""
        finally:
            db.close()
        if npub:
            targets = [npub]
    if not targets:
        return
    nsec = keystore.get_operator_nsec()
    if not nsec:
        return
    sk = nostr_service.decode_seckey(nsec)
    port = _relay_port()
    for t in targets:
        try:
            hexpk = nostr_service.to_pubkey_hex(t)
            if not hexpk:
                continue
            ok, err = await publish_event(port, nip17.wrap(sk, hexpk, text))
            if not ok:
                logger.warning("[uptime] alert DM to %s not published: %s", t[:16], err)
        except Exception as e:
            logger.warning("[uptime] alert DM to %s failed: %s", t[:16], e)


# ---- the tick -----------------------------------------------------------

async def run_checks(force: bool = False) -> dict:
    """Check every monitor that is DUE (or all of them when `force`), fold in the results, alert on
    transitions and persist.

    Called from the WORKER process only (app/worker.py). It is the sole writer of the state doc, so
    there is no cross-process read-modify-write race on it — the app process only ever reads."""
    from app.services import settings_store
    if not settings_store.is_hydrated():
        # An un-hydrated cache returns "" for `uptime_monitors`, which parses as "the admin deleted
        # every monitor" — and the prune below would then drop the whole history. Same trap the
        # blossom whitelist fell into; is_hydrated() exists precisely to tell the two apart.
        logger.debug("[uptime] settings not hydrated yet — skipping this cycle")
        return {"checked": 0, "monitors": 0, "error": "settings not hydrated"}
    await _load()
    if not _loaded:
        # Without the restored state we don't know any monitor's previous status, so every check would
        # look like a first observation: no persist (guarded above) and a recovery would never be
        # reported. Skip the cycle and try again on the next tick — the relay is usually just booting.
        logger.debug("[uptime] state not restored yet — skipping this cycle")
        return {"checked": 0, "monitors": 0, "error": "state unavailable"}
    monitors = parse_monitors()
    live = {m["id"] for m in monitors}
    for gone in [k for k in _state if k not in live]:
        _state.pop(gone, None)                      # a monitor removed from settings leaves the page
    if not monitors:
        await _persist(force=True)
        return {"checked": 0, "monitors": 0}

    timeout = float(max(1, settings_store.get_int("uptime_timeout_seconds", 15)))
    retries = max(1, settings_store.get_int("uptime_retries", 2))
    now = int(time.time())
    due = [m for m in monitors
           if force or (now - int((_state.get(m["id"]) or {}).get("last", 0))) >= m["interval"]]
    if not due:
        return {"checked": 0, "monitors": len(monitors)}

    sem = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def one(m):
        async with sem:
            return m, await check_one(m, timeout)

    results = await asyncio.gather(*(one(m) for m in due), return_exceptions=True)
    transitions = []
    for item in results:
        if isinstance(item, BaseException):
            continue
        m, res = item
        rec, transition = _record(m, res, retries)
        _state[m["id"]] = rec
        if transition:
            transitions.append((rec, transition))

    for rec, kind in transitions:
        logger.info("[uptime] %s is %s (%s)", rec["name"], kind.upper(), rec.get("err") or "ok")
        await _alert(rec, kind)

    await _persist(force=bool(transitions))
    return {"checked": len(due), "monitors": len(monitors), "transitions": len(transitions)}


# ---- read side (app process) --------------------------------------------

def _summarize(rec: dict) -> dict:
    """Shape one record for the client: heartbeats + the 24h / 30d uptime percentages, computed here
    so every viewer shares one calculation and the page stays a dumb renderer."""
    checks = [c for c in (rec.get("checks") or []) if isinstance(c, (list, tuple)) and len(c) >= 3]
    daily = rec.get("daily") or {}
    hourly = rec.get("hourly") or {}
    days = sorted(daily.keys())
    # The last 24 hour-buckets, by key rather than by count — an hour with no checks has no bucket,
    # so "the last 24 entries" could silently reach back further than 24 hours after an outage.
    cutoff = time.strftime("%Y-%m-%dT%H", time.gmtime(time.time() - 86400))
    hours = [h for h in sorted(hourly.keys()) if h >= cutoff]

    def _pct(store, keys):
        ok = sum(int((store.get(k) or [0, 0])[0]) for k in keys)
        total = sum(int((store.get(k) or [0, 0])[1]) for k in keys)
        return round(100.0 * ok / total, 2) if total else None

    return {
        "id": rec.get("id"), "name": rec.get("name"), "url": rec.get("url"),
        "status": rec.get("status", "pending"), "last": rec.get("last", 0),
        "ms": rec.get("ms", 0), "err": rec.get("err", ""), "since": rec.get("since", 0),
        "interval": rec.get("interval", 60),
        "checks": checks[-_KEEP_CHECKS:],
        "avg_ms": int(sum(c[2] for c in checks) / len(checks)) if checks else 0,
        "uptime_24h": _pct(hourly, hours),
        "uptime_30d": _pct(daily, days),
        "days": [[d, (daily.get(d) or [0, 0, 0])[0], (daily.get(d) or [0, 0, 0])[1]] for d in days],
    }


async def get_status(force: bool = False) -> dict:
    """The public payload for `/client/uptime`, cached for _READ_TTL seconds.

    Reads the relay doc rather than `_state`: the checks run in the worker process, so in the APP
    process `_state` is empty by definition. In the worker (the "Check now" path) the doc has just
    been written, so both processes see the same thing.
    """
    from app.services import settings_store
    if not settings_store.get_bool("uptime_enabled", False):
        return {"enabled": False, "monitors": [], "ttl": int(_READ_TTL)}

    nowf = time.monotonic()
    if not force and _read_cache["data"] is not None and (nowf - _read_cache["at"]) < _READ_TTL:
        return _read_cache["data"]

    monitors, updated, read_ok = [], 0, False
    try:
        from app.database import SessionLocal
        from app.services import nostr_store
        db = SessionLocal()
        try:
            sk = settings_store._operator_seckey(db)
        finally:
            db.close()
        if sk:
            doc = await nostr_store.get_doc(_relay_port(), DOC, seckey=sk, strict=True)
            read_ok = True                          # a missing doc is a valid answer: nothing checked yet
            if isinstance(doc, dict):
                updated = int(doc.get("updated") or 0)
                mons = doc.get("monitors") or {}
                if isinstance(mons, dict):
                    monitors = [_summarize(v) for v in mons.values() if isinstance(v, dict)]
    except Exception as e:
        logger.warning("[uptime] status read failed: %s", e)

    if not read_ok and _read_cache["data"] is not None:
        # Serve the last good snapshot rather than an empty list. "No endpoints are being monitored"
        # is a specific, wrong claim to make because one relay query hiccupped — and on a status page
        # it reads as "the monitoring itself is broken".
        return _read_cache["data"]

    monitors.sort(key=lambda m: (m["status"] != "down", (m["name"] or "").lower()))
    up = sum(1 for m in monitors if m["status"] == "up")
    down = sum(1 for m in monitors if m["status"] == "down")
    data = {"enabled": True, "updated": updated, "ttl": int(_READ_TTL),
            "monitors": monitors, "up": up, "down": down, "total": len(monitors)}
    _read_cache["at"] = time.monotonic()
    _read_cache["data"] = data
    return data


# ---- public status page -------------------------------------------------

def _ago(ts: int) -> str:
    if not ts:
        return "never"
    s = max(0, int(time.time()) - int(ts))
    for limit, div, unit in ((60, 1, "s"), (3600, 60, "m"), (86400, 3600, "h")):
        if s < limit:
            return f"{s // div}{unit} ago"
    return f"{s // 86400}d ago"


def _dur(ts: int) -> str:
    if not ts:
        return "—"
    s = max(0, int(time.time()) - int(ts))
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _pct_str(v) -> str:
    return "—" if v is None else (f"{v:.0f}%" if v >= 99.95 else f"{v:.2f}%")


def status_view(data: dict) -> dict:
    """Display-ready shape for the public status page, so the template stays dumb.

    Python owns the wording and the formatting here for the same reason `_render_board` does in
    logs_scheduler: a status page's job is to be unambiguous, and that's easier to guarantee in one
    place than across template conditionals.
    """
    mons = data.get("monitors") or []
    down = int(data.get("down") or 0)
    total = int(data.get("total") or 0)
    rows = []
    for m in mons:
        st = m.get("status") if m.get("status") in ("up", "down") else "pending"
        rows.append({
            "name": m.get("name") or m.get("url") or "",
            "url": m.get("url") or "",
            "status": st,
            "label": {"up": "Up", "down": "Down"}.get(st, "Pending"),
            "pct24": _pct_str(m.get("uptime_24h")),
            "pct30": _pct_str(m.get("uptime_30d")),
            "ms": int(m.get("ms") or 0),
            "avg_ms": int(m.get("avg_ms") or 0),
            "since": _dur(m.get("since") or 0),
            "last": _ago(m.get("last") or 0),
            "err": m.get("err") or "",
            # Newest on the right, capped at 60 — the same bar the in-app tab draws.
            "beats": [{"ok": bool(c[1]),
                       "title": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(c[0])) +
                                (f" · {int(c[2])} ms" if c[1] else " · failed")}
                      for c in (m.get("checks") or [])[-60:]],
        })
    return {
        "ok": down == 0 and total > 0,
        "empty": total == 0,
        "banner": (f"All {total} endpoint{'' if total == 1 else 's'} operational" if down == 0 and total
                   else f"{down} of {total} endpoint{'' if total == 1 else 's'} down" if total
                   else "No endpoints are being monitored yet"),
        "total": total, "up": int(data.get("up") or 0), "down": down,
        "monitors": rows,
        "updated": _ago(int(data.get("updated") or 0)),
    }


# ---- scheduler ----------------------------------------------------------

async def _tick():
    from app.services import settings_store
    if not settings_store.get_bool("uptime_enabled", False):
        return
    try:
        await run_checks()
    except Exception as e:
        logger.warning("[uptime] tick failed: %s", e)


def start_uptime_scheduler():
    """Register the base tick. It self-gates on `uptime_enabled` and re-reads the monitor list every
    tick, so adding an endpoint (or turning the feature on) needs no restart — though it does need the
    worker's periodic settings re-hydrate (app/worker.py, every 120s) to see the change first, so the
    first results can be up to ~2 minutes behind the Save."""
    global uptime_scheduler
    if uptime_scheduler is not None:
        return
    uptime_scheduler = AsyncIOScheduler()
    uptime_scheduler.add_job(_tick, IntervalTrigger(seconds=_TICK), id="uptime",
                             name="Uptime monitors", replace_existing=True,
                             coalesce=True, max_instances=1, misfire_grace_time=_TICK)
    uptime_scheduler.start()
    logger.info("[uptime] scheduler started (base tick %ds)", _TICK)


def stop_uptime_scheduler():
    global uptime_scheduler
    if uptime_scheduler is not None:
        try:
            uptime_scheduler.shutdown()
        except Exception:
            pass
        uptime_scheduler = None
        logger.info("[uptime] scheduler stopped")
