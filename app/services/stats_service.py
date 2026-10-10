"""Public server statistics for the client's Server Stats page.

Everything here is READ-ONLY aggregation over data the node already stores. The relay-derived figures (chiefly
counts over its events) are computed BY THE RELAY PROCESS from its own store -- PosterChanDB when it serves,
Postgres otherwise -- and asked for over its control dir (nostr_relay/aggregates.py, #161). This module defines
what each figure MEANS (the constants below, which aggregates.py counts by) and shapes the answer for the page;
it opens no database. A relay that cannot be asked makes those figures UNKNOWN on the page, never zero.

Scope: the Network-section figures are THIS SERVER's own activity (origin='direct' — see _LOCAL), not
the federated network the relay syncs. ~96% of stored events are synced content (origin='wot'/'ancestor')
or our fedi mirror ('bridge'); counting all of it read as "misleading" since the page frames itself
as "what this node is doing".

The `relay` block (see _relay) answers "what is the relay PROCESS doing" — outbound queue depth, which upstream
streams are live, what it accepted or turned away — from the status file it publishes every 15s.

Cost discipline:

* Every window is bounded by an INTEGER epoch computed in Python (an `extract(epoch from now())` bound cost a
  sequential scan: 646ms vs 8ms on Postgres).
* Results are cached for _TTL seconds here AND in the relay, and served to every viewer from that one snapshot,
  so the cost is per-minute, not per-visitor. The relay computes on its own "relay-stats" thread -- never a read
  worker -- and on PosterChanDB counts copies of its columns taken in short pieces under the store lock.

Calls are the one metric with no history to read: kind-25050 signaling is ephemeral (NIP-01 20000-
29999), so the relay stores none of it — `SELECT count(*) FROM events WHERE kind=25050` is 0 by
design. `bump_call()` counts them as they happen, and the daily totals are persisted to the relay
itself as a kind-30078 doc rather than a new SQL table (this codebase stores new-feature state as
relay events).
"""
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

_TTL = 60.0                 # seconds a computed snapshot is served to everyone
_ASK_TIMEOUT = 20.0         # seconds to wait for the relay's answer before the page says "unknown"
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

# The encrypted AI-chat transcript events, counted by their d-tag prefix and never read.
CHAT_PREFIX = "pcai:msg:"
# Monero support is a public kind-1 tip note carrying this tag (not a Lightning receipt).
MONERO_TIP = ("t", "monerotip")

# Windows: (key, seconds back, bucket size). 61 points, 25 points, 31 points — small enough to draw
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

# A PUBKEY IS NOT ALWAYS A PERSON, and on this relay the exceptions outnumber the people.
#
# * kind 1059 (NIP-59 gift wrap, i.e. every DM) is signed by a **fresh throwaway key per message**
#   — that is the whole point of the wrapper. Counting distinct pubkeys therefore counted one extra
#   "person" per DM ever sent here. Measured on poster.place 2026-09-12: 321 "people active" in 24h
#   of which **238 were gift wraps** (real: 81), and 19,347 over 30 days of which **16,590 were gift
#   wraps** (real: 2,727) — a headcount that grows with message volume, on a node with 128 registered
#   names.
# * kind 9735 (zap receipt) is signed by the LNURL service that settled the payment, not by either
#   the zapper or the zapped. It is a machine, and always the same handful of them.
#
# Only the DISTINCT-PUBKEY counts use this. Event totals are untouched: those events really did
# happen here, and a DM is real activity — it just isn't a new neighbour.
_ONE_TIME_KINDS = (1059, 9735)
_PERSON_PUBKEY = "CASE WHEN kind NOT IN (%s) THEN pubkey END" % ", ".join(str(k) for k in _ONE_TIME_KINDS)


def _relay_counts():
    """Every relay-derived number, computed BY THE RELAY from its own store (nostr_relay/aggregates.py) -- the app
    no longer opens the relay's database. None when the relay could not be asked: every figure that depends on it
    is then UNKNOWN (rendered "—"), never 0."""
    try:
        from app.services.nostr_relay import aggregates
        return aggregates.ask("server-stats", {}, timeout=_ASK_TIMEOUT)
    except Exception as e:      # relay_reader.Unavailable, or anything else that means "could not ask"
        logger.info("[stats] relay counts unavailable: %s", e)
        return None


def _series(raw, now: int):
    """Per-window series {window: {metric: [counts...]}} aligned to fixed buckets, from the relay's raw
    (bucket, kind, n) rows. Counts only locally-published events (origin='direct', see _LOCAL).

    A part the relay could not count is None here (its cards show "—"), and so is every window when `raw` is
    None -- a missing answer must not be drawn as a flat line at zero."""
    out = {}
    for key, span, step in WINDOWS:
        # Exact rolling bounds with partial first/last buckets. Include the current
        # bucket without discarding valid activity at the oldest edge of the window.
        start = now - span
        first_bucket = (start // step) * step
        n = (now // step) - (first_bucket // step) + 1
        buckets = [first_bucket + i * step for i in range(n)]
        index = {b: i for i, b in enumerate(buckets)}
        w = ((raw or {}).get("windows") or {}).get(key) or {}
        series = {m: [0] * n for m in (*KINDS, "monero_zaps")}
        if w.get("kinds") is None:
            for m in KINDS:
                series[m] = None
        else:
            for bucket, kind, count in w["kinds"]:
                i = index.get(int(bucket))
                metric = _KIND_TO_METRIC.get(int(kind))
                if i is not None and metric:
                    series[metric][i] += int(count)
        # Monero support is a public kind-1 tip note, not a Lightning receipt (9735): counted once per event
        # even if its publisher repeats the hashtag; Notes stay intact. No wallet is inspected or inferred.
        if w.get("monero") is None:
            series["monero_zaps"] = None
        else:
            for bucket, count in w["monero"]:
                i = index.get(int(bucket))
                if i is not None:
                    series["monero_zaps"][i] = int(count)
        # Per-window totals so the range selector applies to the summary sections too.
        by_game = w.get("by_game")
        games = None if by_game is None else int(sum(by_game.values()))
        out[key] = {"t0": first_bucket, "step": step, "n": n, "series": series,
                    "totals": {"events": w.get("events"), "people": w.get("people"),
                               "games": games,
                               "by_game": {g: int(by_game.get(g, 0)) for g in GAME_PREFIXES} if by_game is not None else {}}}
    return out


def _games(raw):
    """Distinct game boards per game, all time (one distinct d-tag is one game)."""
    g = (raw or {}).get("games")
    if g is None:
        return {"by_game": {}, "total": None}
    by_game = {name: int(g.get(name, 0)) for name in GAME_PREFIXES}
    return {"by_game": by_game, "total": int(sum(by_game.values()))}


def _origins(raw):
    """{origin: {"total": n, "day": n}} -- every stored event by how it got here; None = unknown. It also feeds
    `events` / `events_24h` in _totals (origin='direct')."""
    o = (raw or {}).get("origins")
    return None if o is None else {str(k): {"total": int(v.get("total", 0)), "day": int(v.get("day", 0))}
                                   for k, v in o.items()}


def _relay(now: int, origins: dict) -> dict:
    """The relay's own activity: what it is doing right now, not what has been posted through it.

    Everything except the store breakdown comes from the relay subprocess's status FILE (it runs in
    its own process — see nostr_relay/thread.py), so this is a file read, not a query. A relay that
    is down, or one still running an older build, simply reports fewer keys; the page renders what
    it is given rather than filling the gaps with zeros, because "0 queued" and "not reported" are
    different facts and only one of them is reassuring.
    """
    st = {}
    try:
        from app.services.nostr_relay.thread import relay_status
        st = relay_status() or {}
    except Exception as e:
        logger.debug("[stats] relay status unavailable: %s", e)
    fh = st.get("firehose") or []
    started = int(st.get("started") or 0)
    out = {
        "running": bool(st.get("running")),
        "members": int(st.get("members", 0) or 0),      # web-of-trust size
        "conns": int(st.get("conns", 0) or 0),          # raw sockets
        "online": int(st.get("online", 0) or 0),        # deduped by IP = people
        # How many of those addresses are THIS NODE's own machines (the app's LAN address, another
        # node, the proxy) rather than people. They stay inside `online` — a LAN-only instance has no
        # other kind of client, so subtracting them would report 0 people to a house full of them —
        # so the only way the figure can be checked is to say how many there are. None = a relay on
        # an older build that doesn't report it, which must render as "not reported", never as 0.
        "online_internal": st.get("online_internal"),
        "subs": st.get("subs"),                         # open REQ subscriptions (None = not reported)
        "accepted": st.get("accepted"),
        "rejected": st.get("rejected"),
        "uptime": (int(now - started) if started else None),
        "outbox": st.get("outbox"),
        "private_outbox": st.get("private_outbox"),
        # One row per live upstream stream. URLs are the public relays this node syncs from (already
        # advertised in its NIP-65/NIP-11 posture), never the private mirror's targets.
        "firehose": [{"relay": r.get("relay"), "label": r.get("label"), "connected": bool(r.get("connected")),
                      "events": int(r.get("events", 0) or 0), "since": int(r.get("since", 0) or 0)}
                     for r in fh if isinstance(r, dict)],
        "firehose_up": sum(1 for r in fh if isinstance(r, dict) and r.get("connected")),
        "origins": origins,
        "prune": st.get("prune") or None,
        "block_purge": st.get("block_purge") or None,
        "stale": (int(now - int(st.get("ts") or 0)) if st.get("ts") else None),
    }
    return out


def _totals(raw, origins):
    """All-time / 24h figures. Network-section counts are scoped to origin='direct' (see _LOCAL): posted HERE,
    not synced from the federated network. AI chat is counted from the ENCRYPTED transcript events by their
    d-tag (never read) -- ~half are assistant replies, so it counts TURNS. `db_bytes` is the relay store's
    genuine on-disk footprint. Unknown (None) whenever the relay could not count it."""
    raw = raw or {}
    direct = (origins or {}).get("direct") if origins is not None else None
    def get(k):
        v = raw.get(k)
        return None if v is None else int(v)
    return {
        "events":        None if origins is None else int((direct or {}).get("total", 0)),
        "events_24h":    None if origins is None else int((direct or {}).get("day", 0)),
        "notes":         get("notes"),
        "streams":       get("streams"),
        "pubkeys_24h":   get("pubkeys_24h"),
        "pubkeys_30d":   get("pubkeys_30d"),
        "profiles":      get("profiles"),
        "ai_requests":   get("ai_requests"),
        "ai_requests_24h": get("ai_requests_24h"),
        "db_bytes":      get("db_bytes"),
    }


def _chat_series(raw, now: int):
    """Daily AI-chat turns for the 30-day window (UTC days), from the relay's per-day counts of the encrypted
    `pcai:msg:` transcript events."""
    days = [time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400)) for i in range(29, -1, -1)]
    rows = (raw or {}).get("chat_daily")
    if rows is None:
        return {"series": None, "days": days, "unknown": True}
    counts = {d: 0 for d in days}
    for day, n in rows:
        d = time.strftime("%Y-%m-%d", time.gmtime(int(day)))
        if d in counts:
            counts[d] = int(n)
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
    """The blocking half (runs in a worker thread): ONE ask to the relay process, which counts from its own
    store (PosterChanDB or Postgres) on its own thread, plus local file reads. No database is opened here."""
    t0 = time.monotonic()
    raw = _relay_counts()
    now = int((raw or {}).get("now") or time.time())     # the relay's buckets are aligned to ITS now
    origins = _origins(raw)
    data = {
        "now": now,
        "windows": _series(raw, now),
        "games": _games(raw),
        "totals": _totals(raw, origins),
        "relay": _relay(now, origins),
        "counters": _counter_series(int(time.time())),
        "chat": _chat_series(raw, now),
        "ttl": int(_TTL),
        # Said on the page: the relay could not be asked, so its numbers are unknown -- not zero.
        "relay_unavailable": raw is None,
        "relay_backend": (raw or {}).get("backend"),
    }
    data["ms"] = int((time.monotonic() - t0) * 1000)
    return data


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
        # An unanswered ask is cached briefly only, so the page recovers as soon as the relay does.
        _cache["at"] = time.monotonic() - (0 if not data.get("relay_unavailable") else max(0.0, _TTL - 10))
        _cache["data"] = data
        return data
