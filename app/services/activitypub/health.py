"""Is outbound federation MOVING? Measured by the worker after every delivery tick, shown in Admin →
Fediverse and on the system-health board, and alerted to the admin ONCE when it stops and once when
it recovers.

2026-10-11: outbound delivery stalled for ~23 hours and nobody was told. The only traces were
"delivery tick failed: TimeoutError" in `journalctl -u posterchanai-worker.service` (NOT the app's
journal) and a `last_error` string in this document that only the Admin panel ever showed. So this
measures the thing a user notices -- a member's post that has not gone out -- rather than whether the
loop is running:

  * the CANARY: the oldest federatable member event NEWER than the delivery cursor (one relay query,
    no test content posted anywhere). Everything at or behind the cursor was handled; anything after
    it is waiting. A quiet node has nothing after the cursor and is "caught up" -- the cursor's own
    age is never the measure, since it does not move when nobody posts;
  * consecutive failed ticks (an exception, the worker's time limit, or a tick that could not run at
    all -- relay unreadable, settings not loaded); a tick that ran out of its budget while making
    progress is not a failure, the canary sees whether it keeps up;
  * sends per minute, from the delivered counter.

Everything is COUNTS and timestamps: no post content is ever read into this document or logged.
"""
from __future__ import annotations

import logging
import time

from app.services import nostr_store, settings_store
from app.services.activitypub import config

logger = logging.getLogger(__name__)

DOC = "pcai:ap:stats"
LAG_ALERT = 15 * 60          # a member event unsent this long = delivery is behind
FAILS_ALERT = 5              # this many failed ticks in a row = delivery is failing
STALE_REPORT = 15 * 60       # no report from the worker this long, while on = nobody is delivering
WRITE_EVERY = 300            # the document is rewritten at least this often (so staleness means something)
ALERT_RETRY = 600            # an alert no channel took is tried again after this long
CANARY_KINDS = [1, 1111, 1068, 6, 7, 1018, 1984, 5]
_CANARY_LIMIT = 200
WORKER_LOG = "journalctl -u posterchanai-worker.service"

_mem: dict = {"fails": 0, "why": "", "alerted": None, "stall_since": 0, "rate": [], "wrote": 0.0,
              "last_key": None, "retry_at": 0}


def reset() -> None:
    _mem.update(fails=0, why="", alerted=None, stall_since=0, rate=[], wrote=0.0, last_key=None, retry_at=0)


def fmt_dur(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{s // 60}m"
    if s < 48 * 3600:
        h, m = divmod(s // 60, 60)
        return f"{h}h" + (f" {m}m" if m and h < 10 else "")
    d, h = divmod(s // 3600, 24)
    return f"{d}d" + (f" {h}h" if h else "")


async def oldest_unsent(cursor: int, members: list) -> int | None:
    """`created_at` of the oldest federatable member event after `cursor`; 0 when there is none; None
    when the relay could not be asked ("could not ask" is never "caught up")."""
    from app.services.activitypub import outbox
    if not members or not cursor:
        return 0
    try:
        evs = await nostr_store._ws_query(settings_store._port(), [{"kinds": CANARY_KINDS, "authors": members,
                                                                    "since": int(cursor) + 1, "limit": _CANARY_LIMIT}],
                                          strict=True)
    except Exception:
        return None
    real = [e for e in evs if isinstance(e, dict) and not outbox._is_mirror(e) and not outbox._protected(e)
            and not outbox._inert_deletion(e) and e.get("id") not in outbox._seen]
    if not real:
        return 0
    if len(evs) >= _CANARY_LIMIT:
        return int(cursor) + 1           # more waiting than one read shows: the oldest is no newer than this
    return min(int(e.get("created_at") or 0) for e in real)


def assess(*, now: int, enabled: bool, oldest: int | None, fails: int) -> dict:
    """The verdict, from measurements only. state: off | ok | behind | failing | unknown."""
    lag = max(0, now - oldest) if oldest else 0
    if not enabled:
        state = "off"
    elif fails >= FAILS_ALERT:
        state = "failing"
    elif oldest is None:
        state = "unknown"
    elif lag > LAG_ALERT:
        state = "behind"
    else:
        state = "ok"
    return {"state": state, "lag": lag, "fails": fails}


def view(doc: dict | None, now: int | None = None) -> dict:
    """{"ok": True/False/None, "state", "text"} for a stats document -- ONE wording for Admin →
    Fediverse and the health board. A document the worker stopped rewriting is itself red."""
    now = int(now or time.time())
    doc = doc if isinstance(doc, dict) else {}
    h = doc.get("health") if isinstance(doc.get("health"), dict) else None
    if not h:
        return {"ok": None, "state": "unknown", "text": "Delivery: no report from the delivery worker yet"}
    state = h.get("state") or "unknown"
    err = str(h.get("error") or doc.get("last_error") or "").strip()
    tail = f" — last error {err}" if err else ""
    age = now - int(doc.get("at") or 0)
    if state != "off" and age > STALE_REPORT:
        return {"ok": False, "state": "silent",
                "text": f"Delivery: no report from the delivery worker for {fmt_dur(age)} — {WORKER_LOG}"}
    if state == "off":
        return {"ok": None, "state": state, "text": "Delivery: off"}
    if state == "ok":
        rate = h.get("per_min") or 0
        return {"ok": True, "state": state,
                "text": "Delivery: caught up" + (f" · {rate:g} sent/min" if rate else "")}
    if state == "behind":
        return {"ok": False, "state": state, "text": f"Delivery {fmt_dur(h.get('lag') or 0)} behind{tail}"}
    if state == "failing":
        return {"ok": False, "state": state,
                "text": f"Delivery failing — {int(h.get('fails') or 0)} ticks in a row{tail}"}
    return {"ok": None, "state": state, "text": "Delivery: could not check (the relay did not answer)"}


def board_row(doc: dict | None, now: int | None = None) -> tuple[str, str]:
    """(status, detail) for the system-health board's measured Federation row."""
    v = view(doc, now)
    status = "green" if v["ok"] else "red" if v["ok"] is False else ("none" if v["state"] == "off" else "yellow")
    return status, v["text"].replace("Delivery: ", "").replace("Delivery ", "")


def decide(alerted: bool, state: str) -> str:
    """"stall" when it has just stopped (and nobody was told), "recovered" when it has just caught up
    again (and somebody was told), "" otherwise. An unknown reading changes nothing either way."""
    stalled = state in ("behind", "failing")
    if stalled and not alerted:
        return "stall"
    if state == "ok" and alerted:
        return "recovered"
    return ""


def alert_text(kind: str, h: dict, *, stalled_for: int = 0) -> str:
    site = (settings_store.get("site_name", "") or config.domain() or "this server").strip()
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    if kind == "stall":
        what = (f"{fmt_dur(h.get('lag') or 0)} behind" if h.get("state") == "behind"
                else f"{int(h.get('fails') or 0)} delivery ticks failed in a row")
        err = h.get("error") or ""
        return (f"🔴 FEDERATION STALLED — {site}\nOutbound ActivityPub delivery is {what}: members' posts, "
                f"likes and replies are not reaching the fediverse."
                + (f"\nLast error: {err}" if err else "")
                + f"\nWorker log: {WORKER_LOG}\n{when}")
    return (f"🟢 FEDERATION RECOVERED — {site}\nOutbound ActivityPub delivery has caught up"
            + (f" (stalled for {fmt_dur(stalled_for)})" if stalled_for else "") + f".\n{when}")


def alerts_on() -> bool:
    """`activitypub_alerts` -- ON unless explicitly off (a blank row must not silence the alarm)."""
    return config._on_unless_off("activitypub_alerts")


async def _send(text: str) -> bool | None:
    """True = a channel took it; None = nowhere to send it; False = every channel tried failed."""
    from app.services import uptime_service
    uptime_service._refresh_settings()
    res = await uptime_service.send_admin_alert(text, telegram=True, nostr=True, tag="federation health")
    vals = [v for v in res.values() if v is not None]
    if any(vals):
        return True
    return False if vals else None


async def after_tick(stats: dict, *, ok: bool, why: str = "", now: int | None = None) -> dict:
    """Measure, decide, alert, and write the stats document. Called by the worker after EVERY tick
    (success or not). Returns the health dict. Never raises."""
    from app.services.activitypub import outbox, state as ap_state
    now = int(now or time.time())
    enabled = config.enabled() and bool(config.base_url())
    if ok:
        _mem.update(fails=0, why="")
    else:
        _mem["fails"] += 1
        _mem["why"] = why[:200]
    prior = None
    if _mem["alerted"] is None:                   # first tick of this process: what was the admin told?
        try:
            prior = await nostr_store.get_doc(settings_store._port(), DOC,
                                              seckey=settings_store._operator_seckey(None), strict=True) or {}
        except Exception:
            prior = {}
        _mem["alerted"] = bool(prior.get("alerted"))
        _mem["stall_since"] = int(prior.get("stall_since") or 0)
    oldest = 0
    if enabled:
        try:
            cursor = int((await ap_state._get(ap_state.CURSOR) or {}).get("since") or 0)
            oldest = await oldest_unsent(cursor, await outbox._members())
        except Exception:
            oldest = None
    delivered = int(stats.get("delivered") or 0)
    _mem["rate"] = [(t, d) for t, d in _mem["rate"] if now - t <= 600] + [(now, delivered)]
    t0, d0 = _mem["rate"][0]
    per_min = round((delivered - d0) * 60 / (now - t0), 1) if now - t0 >= 60 and delivered >= d0 else 0
    h = assess(now=now, enabled=enabled, oldest=oldest, fails=_mem["fails"])
    h.update(per_min=per_min, oldest_unsent=oldest or 0, checked=now,
             error=(_mem["why"] or stats.get("last_error") or "") if h["state"] != "ok" else "")
    if h["state"] in ("behind", "failing"):
        _mem["stall_since"] = _mem["stall_since"] or (oldest or now)
    action = decide(bool(_mem["alerted"]), h["state"]) if enabled else ""
    if not enabled and _mem["alerted"]:
        _mem.update(alerted=False, stall_since=0)            # turned off: nothing left to recover from
    if action and alerts_on() and now >= _mem["retry_at"]:
        stalled_for = now - _mem["stall_since"] if _mem["stall_since"] else 0
        try:
            took = await _send(alert_text(action, h, stalled_for=stalled_for))
        except Exception as e:
            logger.warning("[activitypub] federation %s alert failed: %s", action, e)
            took = False
        if took is False:
            _mem["retry_at"] = now + ALERT_RETRY            # tried and failed: again later, not every tick
        else:
            if took is None:
                logger.warning("[activitypub] federation %s: no alert channel reaches the admin "
                               "(no linked Telegram, no npub) -- shown in Admin only", action)
            _mem["alerted"] = action == "stall"
            _mem["retry_at"] = 0
            if action == "recovered":
                _mem["stall_since"] = 0
            logger.warning("[activitypub] federation %s (%s)", action, h["state"])
    elif action == "recovered" and not alerts_on():
        _mem.update(alerted=False, stall_since=0)
    if h["state"] == "ok" and not _mem["alerted"]:
        _mem["stall_since"] = 0
    doc = dict(stats, health=h, alerted=bool(_mem["alerted"]), stall_since=int(_mem["stall_since"] or 0), at=now)
    doc.pop("last_tick", None)
    key = (h["state"], bool(_mem["alerted"]), {k: v for k, v in stats.items() if k != "last_tick"})
    last = _mem["last_key"]
    turned = last is None or key[:2] != last[:2]          # the verdict or the alert state changed
    if turned or (enabled and ((key != last and now - _mem["wrote"] >= 60) or now - _mem["wrote"] >= WRITE_EVERY)):
        try:
            await nostr_store.put_doc(settings_store._port(), settings_store._operator_seckey(None), DOC, doc)
            _mem["wrote"], _mem["last_key"] = now, key
        except Exception:
            pass
    return h
