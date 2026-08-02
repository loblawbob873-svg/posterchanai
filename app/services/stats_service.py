"""Public server statistics for the client's Server Stats page.

Everything here is READ-ONLY aggregation over data the node already stores — chiefly the relay's
`events` table, which the app shares (same Postgres database, see app/database.py). No new tables and
no per-request write path: a stats page must never be able to slow down posting.

Scope: the Network-section figures are THIS SERVER's own activity (origin='direct' — see _LOCAL), not
the federated network the relay syncs. ~96% of `events` is synced content (origin='wot'/'ancestor')
or our fedi mirror ('bridge'); counting all of it read as "misleading" since the page frames itself
as "what this node is doing".

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

_COUNTER_KEY = "stats_counters"   # local-only settings key: {"YYYY-MM-DD": {"calls": n, ...}}
# The same events bucketed by UTC HOUR ("YYYY-MM-DDTHH"), which is what makes a real rolling 24h
# possible. The day buckets cannot answer it: "last 24h" was served as the CURRENT UTC day-to-date, so
# at 20:40 in UTC-6 it reported 2.7 hours of activity under a 24-hour label — 8 memes against a 7-day
# average of 51/day, which reads as a broken counter rather than a mislabelled window. Worst right
# after the UTC rollover, which is 18:00 local here, i.e. every evening.
#
# Separate key rather than a nested structure because bump_counter already bounds each counter to its
# last 90 buckets — 90 days for the daily key, and for this one 90 HOURS, which self-prunes well past
# the 24 we read and costs no extra code.
_COUNTER_KEY_H = "stats_counters_hourly"
# Things that leave NO trace to aggregate later, so they can only be counted as they happen:
#   calls  — kind-25050 signaling is ephemeral, the relay stores none of it
#   image/music/video — generated media is returned to the caller, never recorded server-side
#   meme   — same: the Meme Builder streams the rendered MP4 straight back and keeps no row
# (Chat is NOT here: `messages` rows are real history, so chat is aggregated from the table instead
# and keeps its full past rather than starting at zero on the day this shipped.)
COUNTERS = ("calls", "image", "music", "video", "meme")
_counts: dict = {}                # {"YYYY-MM-DD": {metric: n}}
_counts_dirty = False
_counts_loaded = False


def bump(metric: str, n: int = 1) -> None:
    """Record `n` occurrences of `metric` today.

    In-memory and exception-proof by design: these calls sit inside the call-signaling and media
    generation paths, where a slow or failing stats write would be felt as a slow call or a stalled
    image. Persistence happens later, out of band, in flush_counters().
    """
    try:
        if metric not in COUNTERS:
            return
        # Write THROUGH, don't tally in memory: these events are observed by different processes
        # (media generation in the app, call signaling in the worker), so a per-process tally plus a
        # periodic flush counted nothing — each flushed its own empty copy and restarts discarded the
        # rest. That is why Server Stats read 0 images and 0 music after a day of generating.
        from app.services import settings_store
        now = time.gmtime()
        settings_store.bump_counter(_COUNTER_KEY, time.strftime("%Y-%m-%d", now), metric, n)
        # …and the hourly bucket, so "last 24h" can be answered as an actual rolling window instead of
        # as today-so-far. Both are written: the daily series still backs the 30-day chart and keeps
        # the history that predates hourly counting.
        settings_store.bump_counter(_COUNTER_KEY_H, time.strftime("%Y-%m-%dT%H", now), metric, n)
    except Exception:
        pass


def bump_call(n: int = 1) -> None:
    """Back-compat alias used by the kind-25050 subscription."""
    bump("calls", n)


async def _load_counters() -> None:
    """No-op. Counters are written through to the shared local counter file on every bump and read
    from disk on every render, so there is nothing to hydrate. Kept so callers need no change."""
    return


async def flush_counters() -> None:
    """No-op — see _load_counters. The old design tallied in memory and flushed here every 5 minutes,
    which counted NOTHING: the scheduled flush runs in the WORKER while image/music/video generation
    happens in the APP, so each process flushed its own empty copy and a restart discarded the rest."""
    return


async def flush_calls() -> None:
    """Back-compat alias for the scheduled job."""
    return



# "This server", not "the whole network". The relay federates: ~96% of its `events` rows are
# origin='wot'/'ancestor' (content SYNCED from upstream relays) or 'bridge' (our fedi mirror). Only
# origin='direct' rows were PUBLISHED here by this node's own clients — that's what "Server Stats"
# should count, matching the page's own "what this node is doing" framing. This one filter also
# subsumes the bridge-puppet exclusion (puppet events are origin='bridge', never 'direct').
# Applied to the Network-section metrics only; Games / AI / media are already local (pcai: d-tags +
# local counters), and `db_bytes` is genuine on-disk footprint, so those stay as-is.
_LOCAL = "origin = 'direct'"


def _series(db, now: int):
    """One grouped scan per window → {window: {metric: [counts...]}} aligned to fixed buckets.
    Counts only locally-published events (origin='direct', see _LOCAL)."""
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
             WHERE created_at >= :start AND created_at < :now AND kind = ANY(:kinds) AND %s
             GROUP BY 1, 2
        """ % _LOCAL), {"step": step, "start": start, "now": now, "kinds": _ALL_KINDS}).fetchall()
        for bucket, kind, count in rows:
            i = index.get(int(bucket))
            metric = _KIND_TO_METRIC.get(int(kind))
            if i is not None and metric:
                series[metric][i] += int(count)
        # Per-window totals so the range selector actually applies to the summary sections. Without
        # these, Network / Games / AI showed all-time figures that never moved when you switched
        # range, which reads as broken. Measured: 0.01s / 0.08s / 0.50s for the three windows.
        row = db.execute(text("SELECT count(*), count(DISTINCT pubkey) FROM events "
                              "WHERE created_at >= :s AND created_at <= :n AND " + _LOCAL),
                         {"s": start, "n": now}).first()
        win_events, win_people = int(row[0] or 0), int(row[1] or 0)
        # Per-GAME breakdown for this window, one grouped query (~0.01-0.04s) rather than six LIKE
        # counts, so the games bars follow the range selector like everything else does.
        gparams = {"s": start}
        cases, wheres = [], []
        for i, (gname, pre) in enumerate(GAME_PREFIXES.items()):
            gparams["p%d" % i] = pre + "%"
            gparams["n%d" % i] = gname
            cases.append("WHEN t.value LIKE :p%d THEN :n%d" % (i, i))
            wheres.append("t.value LIKE :p%d" % i)
        grows = db.execute(text(
            "SELECT CASE %s END AS g, count(DISTINCT t.value) "
            "FROM event_tags t JOIN events e ON e.id = t.event_id "
            "WHERE t.tag = 'd' AND (%s) AND e.created_at >= :s GROUP BY 1"
            % (" ".join(cases), " OR ".join(wheres))), gparams).fetchall()
        by_game = {g: 0 for g in GAME_PREFIXES}
        for gname, cnt in grows:
            if gname in by_game:
                by_game[gname] = int(cnt or 0)
        out[key] = {"t0": start, "step": step, "n": n, "series": series,
                    "totals": {"events": win_events, "people": win_people,
                               "games": int(sum(by_game.values())), "by_game": by_game}}
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
    # Network-section counts are scoped to origin='direct' (see _LOCAL): posted HERE, not synced from
    # the federated network. `db_bytes` stays the full on-disk footprint (honest storage figure).
    return {
        "events":        scalar("SELECT count(*) FROM events WHERE " + _LOCAL),
        "events_24h":    scalar("SELECT count(*) FROM events WHERE created_at >= :s AND " + _LOCAL, {"s": now - 86400}),
        "notes":         scalar("SELECT count(*) FROM events WHERE kind=1 AND " + _LOCAL),
        "streams":       scalar("SELECT count(*) FROM events WHERE kind=30311 AND " + _LOCAL),
        "pubkeys_24h":   scalar("SELECT count(DISTINCT pubkey) FROM events WHERE created_at >= :s AND " + _LOCAL, {"s": now - 86400}),
        "pubkeys_30d":   scalar("SELECT count(DISTINCT pubkey) FROM events WHERE created_at >= :s AND " + _LOCAL, {"s": now - 2592000}),
        "profiles":      scalar("SELECT count(*) FROM events WHERE kind=0 AND " + _LOCAL),
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
    # Read FROM DISK each time — another process may have counted something since this one started.
    from app.services import settings_store
    counts = settings_store.read_counter(_COUNTER_KEY)
    hours = settings_store.read_counter(_COUNTER_KEY_H)
    days = [time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400)) for i in range(29, -1, -1)]
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    # The 24 hourly buckets ending with the current one — a genuine rolling day, not "since UTC
    # midnight". `h24[0]` is the current (partial) hour, which is also the "last hour" figure.
    h24 = [time.strftime("%Y-%m-%dT%H", time.gmtime(now - i * 3600)) for i in range(0, 24)]
    out = {"days": days, "metrics": {}}
    for m in COUNTERS:
        out["metrics"][m] = {
            "series": [int((counts.get(d) or {}).get(m, 0)) for d in days],
            "total":  int(sum(int((v or {}).get(m, 0)) for v in counts.values())),
            "today":  int((counts.get(today) or {}).get(m, 0)),
            # New windows. A node that has only just started counting hourly reports small numbers
            # here rather than wrong ones — the daily series above still carries the older history.
            "last24": int(sum(int((hours.get(h) or {}).get(m, 0)) for h in h24)),
            "last1h": int((hours.get(h24[0]) or {}).get(m, 0)),
        }
    # True only once the hourly store actually REACHES BACK 24h. Hourly counting starts the moment this
    # ships, so for the first day the window is mostly empty — publishing it then would replace a
    # mislabelled-but-real number with a confident 0, which is a worse lie than the one being fixed.
    # Until it is covered the client falls back to the day bucket AND relabels the card, so the number
    # is never shown under a window it cannot answer. Lexicographic compare is valid: the keys are
    # zero-padded ISO ("2026-08-02T03"). Self-healing — it flips to true 24h after deploy.
    oldest = min(hours) if hours else None
    out["rolling"] = bool(oldest and oldest <= h24[-1])
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
