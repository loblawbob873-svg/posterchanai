"""Public server statistics for the client's Server Stats page.

Everything here is READ-ONLY aggregation over data the node already stores — chiefly the relay's
`events` table, which the app shares (same Postgres database, see app/database.py). No new tables and
no per-request write path: a stats page must never be able to slow down posting.

Cost discipline (the whole reason this module exists rather than inline queries in the router):

* Every window is bounded by an INTEGER epoch computed in Python. Writing
  `created_at >= extract(epoch from now())` instead costs a sequential scan of ~2.2M rows — measured
  at 646ms for the 60-minute window versus 8ms with a plain integer bound, because the extract() form
  isn't a constant the planner can push into idx_events_kind_created.
* Results are cached for _TTL seconds and served to every viewer from that one snapshot, so the cost
  is per-minute, not per-visitor. Measured full refresh: ~0.5s, dominated by the 30-day window.
* The counts are grouped in ONE pass per window (bucket, kind) rather than a query per metric.

Calls are the one metric with no history to read: kind-25050 signaling is ephemeral (NIP-01 20000-
29999), so the relay stores none of it — `SELECT count(*) FROM events WHERE kind=25050` is 0 by
design. `bump_call()` counts them as they happen, and the daily totals are persisted to the relay
itself as a kind-30078 doc rather than a new SQL table (this codebase stores new-feature state as
relay events).
"""
import asyncio
import json
import logging
import time

logger = logging.getLogger(__name__)

_TTL = 60.0                 # seconds a computed snapshot is served to everyone
_cache = {"at": 0.0, "data": None}
_lock = asyncio.Lock()      # one refresh at a time — a burst of viewers must not each run the scans

# Kinds worth charting. Anything not listed still counts toward "all events" totals but gets no
# series of its own — keeping this list short is what keeps the grouped scan cheap.
KINDS = {
    "notes":     [1],
    "reactions": [7],
    "reposts":   [6],
    "replies":   [1111],
    "zaps":      [9735],
    "dms":       [4, 1059],
    "articles":  [30023],
    "profiles":  [0],
    "files":     [1063],
    "streams":   [30311],
}
_ALL_KINDS = sorted({k for ks in KINDS.values() for k in ks})
_KIND_TO_METRIC = {k: name for name, ks in KINDS.items() for k in ks}

# d-tag prefixes the in-client games use for their board events (kind 30078). One distinct d-tag is
# one game, which is why games are counted from event_tags rather than from the event rows.
GAME_PREFIXES = {
    "chess":     "pcai:chesstr:",
    "tictactoe": "pcai:ttt:",
    "hangman":   "pcai:hangman:",
    "connect4":  "pcai:connect4:",
    "blackjack": "pcai:blackjack:",
    "holdem":    "pcai:holdem:",
}

# Windows: (key, seconds back, bucket size). 60 points, 24 points, 30 points — small enough to draw
# as plain SVG polylines with no client-side downsampling.
WINDOWS = (
    ("minute", 3600,    60),
    ("hour",   86400,   3600),
    ("day",    2592000, 86400),
)

_STATS_D = "pcai:stats:counters"   # kind-30078 doc: {"YYYY-MM-DD": {"calls": n, "image": n, ...}}
# Things that leave NO trace to aggregate later, so they can only be counted as they happen:
#   calls  — kind-25050 signaling is ephemeral, the relay stores none of it
#   image/music/video — generated media is returned to the caller, never recorded server-side
# (Chat is NOT here: `messages` rows are real history, so chat is aggregated from the table instead
# and keeps its full past rather than starting at zero on the day this shipped.)
COUNTERS = ("calls", "image", "music", "video")
_counts: dict = {}                # {"YYYY-MM-DD": {metric: n}}
_counts_dirty = False
_counts_loaded = False


def bump(metric: str, n: int = 1) -> None:
    """Record `n` occurrences of `metric` today.

    In-memory and exception-proof by design: these calls sit inside the call-signaling and media
    generation paths, where a slow or failing stats write would be felt as a slow call or a stalled
    image. Persistence happens later, out of band, in flush_counters().
    """
    global _counts_dirty
    try:
        if metric not in COUNTERS:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime())
        _counts.setdefault(day, {})
        _counts[day][metric] = _counts[day].get(metric, 0) + int(n)
        _counts_dirty = True
    except Exception:
        pass


def bump_call(n: int = 1) -> None:
    """Back-compat alias used by the kind-25050 subscription."""
    bump("calls", n)


async def _load_counters() -> None:
    """Read persisted daily counts once, so a restart doesn't reset these charts to zero."""
    global _counts_loaded
    if _counts_loaded:
        return
    _counts_loaded = True     # set first: a failed read must not retry on every request
    try:
        from app.services import settings_store, keystore
        from app.services.nostr_store import get_doc
        from app.services.nostr import nostr_service, bip340
        nsec = keystore.get_operator_nsec()
        if not nsec:
            return
        sk = nostr_service.decode_seckey(nsec)
        if not sk:
            return
        port = settings_store.get_int("nostr_relay_port", 3052)
        # Plaintext doc (encrypt=False on write) — these are public counts, and the page is public.
        doc = await get_doc(port, _STATS_D, pubkey=bip340.pubkey_from_seckey(sk).hex(), encrypt=False)
        if isinstance(doc, dict):
            for day, metrics in doc.items():
                if not isinstance(metrics, dict):
                    continue
                cur = _counts.setdefault(day, {})
                for m, n in metrics.items():
                    try:
                        # max(), not +=: a reload must not double-count what's already in memory.
                        cur[m] = max(int(n), cur.get(m, 0))
                    except Exception:
                        continue
    except Exception as e:
        logger.debug("[stats] counter history load skipped: %s", e)


async def flush_counters() -> None:
    """Persist daily counts to the relay (kind-30078). Cheap, idempotent, safe to skip."""
    global _counts_dirty
    if not _counts_dirty:
        return
    try:
        from app.services import settings_store, keystore
        from app.services.nostr_store import put_doc
        from app.services.nostr import nostr_service
        nsec = keystore.get_operator_nsec()
        if not nsec:
            return
        sk = nostr_service.decode_seckey(nsec)
        if not sk:
            return
        # Keep ~90 days: more than the longest chart needs, bounded so the doc can't grow forever.
        keep = dict(sorted(_counts.items())[-90:])
        port = settings_store.get_int("nostr_relay_port", 3052)
        await put_doc(port, sk, _STATS_D, keep, encrypt=False)
        _counts_dirty = False
    except Exception as e:
        logger.debug("[stats] counter flush failed: %s", e)


async def flush_calls() -> None:
    """Back-compat alias for the scheduled flush job."""
    await flush_counters()


def _series(db, now: int):
    """One grouped scan per window → {window: {metric: [counts...]}} aligned to fixed buckets."""
    from sqlalchemy import text
    out = {}
    for key, span, step in WINDOWS:
        n = span // step
        start = ((now - span) // step) * step          # align to the bucket grid
        buckets = [start + i * step for i in range(n)]
        index = {b: i for i, b in enumerate(buckets)}
        series = {m: [0] * n for m in KINDS}
        rows = db.execute(text("""
            SELECT (created_at / :step) * :step AS bucket, kind, count(*)
              FROM events
             WHERE created_at >= :start AND created_at < :now AND kind = ANY(:kinds)
             GROUP BY 1, 2
        """), {"step": step, "start": start, "now": now, "kinds": _ALL_KINDS}).fetchall()
        for bucket, kind, count in rows:
            i = index.get(int(bucket))
            metric = _KIND_TO_METRIC.get(int(kind))
            if i is not None and metric:
                series[metric][i] += int(count)
        out[key] = {"t0": start, "step": step, "n": n, "series": series}
    return out


def _games(db):
    """Distinct game boards per game. event_tags(tag,value) is indexed, so this is a ~25ms lookup."""
    from sqlalchemy import text
    out, total = {}, 0
    for name, prefix in GAME_PREFIXES.items():
        try:
            n = db.execute(text("""SELECT count(DISTINCT value) FROM event_tags
                                    WHERE tag = 'd' AND value LIKE :p"""),
                           {"p": prefix + "%"}).scalar() or 0
        except Exception:
            n = 0
        out[name] = int(n)
        total += int(n)
    return {"by_game": out, "total": total}


def _totals(db, now: int):
    from sqlalchemy import text
    def scalar(sql, params=None, default=0):
        try:
            v = db.execute(text(sql), params or {}).scalar()
            return int(v) if v is not None else default
        except Exception:
            return default

    # Chat volume from the ENCRYPTED transcript events (the plaintext `messages` table is gone).
    # We count events, never read content — the d-tag identifies them, the body stays encrypted.
    # ~half of the events are assistant replies, so this counts TURNS, not user prompts; the label
    # on the page says "AI chat" rather than "requests" for that reason.
    ai_day = scalar("""SELECT count(*) FROM event_tags t JOIN events e ON e.id = t.event_id
                        WHERE t.tag = 'd' AND t.value LIKE 'pcai:msg:%' AND e.created_at >= :s""",
                    {"s": now - 86400})
    return {
        "events":        scalar("SELECT count(*) FROM events"),
        "events_24h":    scalar("SELECT count(*) FROM events WHERE created_at >= :s", {"s": now - 86400}),
        "notes":         scalar("SELECT count(*) FROM events WHERE kind=1"),
        "streams":       scalar("SELECT count(*) FROM events WHERE kind=30311"),
        "pubkeys_24h":   scalar("SELECT count(DISTINCT pubkey) FROM events WHERE created_at >= :s", {"s": now - 86400}),
        "pubkeys_30d":   scalar("SELECT count(DISTINCT pubkey) FROM events WHERE created_at >= :s", {"s": now - 2592000}),
        "profiles":      scalar("SELECT count(*) FROM events WHERE kind=0"),
        "ai_requests":   scalar("""SELECT count(*) FROM event_tags
                                    WHERE tag = 'd' AND value LIKE 'pcai:msg:%'"""),
        "ai_requests_24h": ai_day,
        "db_bytes":      scalar("SELECT pg_database_size(current_database())"),
    }


def _chat_series(db, now: int):
    """Daily AI-chat requests for the 30-day window, straight from `messages`.

    Chat is the one AI metric with real history — every prompt is already a row — so it's aggregated
    rather than counted forward like image/music/video. `created_at` is a naive UTC timestamp here,
    matching the rest of the app.
    """
    from sqlalchemy import text
    days = [time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400)) for i in range(29, -1, -1)]
    counts = {d: 0 for d in days}
    try:
        rows = db.execute(text("""
            SELECT to_char(to_timestamp(e.created_at) at time zone 'utc', 'YYYY-MM-DD') AS d, count(*)
              FROM event_tags t JOIN events e ON e.id = t.event_id
             WHERE t.tag = 'd' AND t.value LIKE 'pcai:msg:%' AND e.created_at >= :since
             GROUP BY 1
        """), {"since": now - 2592000}).fetchall()
        for d, n in rows:
            if d in counts:
                counts[d] = int(n)
    except Exception as e:
        logger.debug("[stats] chat series unavailable: %s", e)
    return {"series": [counts[d] for d in days], "days": days}


def _counter_series(now: int):
    """Daily series for every counted metric, plus totals. 30 days to match the day window."""
    days = [time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400)) for i in range(29, -1, -1)]
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    out = {"days": days, "metrics": {}}
    for m in COUNTERS:
        out["metrics"][m] = {
            "series": [int(_counts.get(d, {}).get(m, 0)) for d in days],
            "total":  int(sum(v.get(m, 0) for v in _counts.values())),
            "today":  int(_counts.get(today, {}).get(m, 0)),
        }
    # Said out loud on the page: these counters start when the feature ships, unlike the relay-derived
    # series which are historical. A silent 0 would read as "nobody uses this".
    out["since_deploy"] = True
    return out


def _compute() -> dict:
    """The blocking half: opens its OWN session because it runs in a worker thread.

    Never call this on the event loop. It is ~1.2s of synchronous SQL (dominated by the 30-day scan
    and the two count(DISTINCT pubkey) queries), which on the loop would stall every websocket,
    stream and chat request on the node for that whole second.
    """
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        now = int(time.time())
        t0 = time.monotonic()
        data = {
            "now": now,
            "windows": _series(db, now),
            "games": _games(db),
            "totals": _totals(db, now),
            "counters": _counter_series(now),
            "chat": _chat_series(db, now),
            "ttl": int(_TTL),
        }
        data["ms"] = int((time.monotonic() - t0) * 1000)
        return data
    finally:
        db.close()


async def get_stats(force: bool = False) -> dict:
    """Cached public stats payload. Every viewer in a _TTL window shares one computation."""
    nowf = time.monotonic()
    if not force and _cache["data"] is not None and (nowf - _cache["at"]) < _TTL:
        return _cache["data"]
    async with _lock:
        # Re-check inside the lock: while we waited, another request may have refreshed it.
        nowf = time.monotonic()
        if not force and _cache["data"] is not None and (nowf - _cache["at"]) < _TTL:
            return _cache["data"]
        await _load_counters()
        data = await asyncio.to_thread(_compute)
        _cache["at"] = time.monotonic()
        _cache["data"] = data
        return data
