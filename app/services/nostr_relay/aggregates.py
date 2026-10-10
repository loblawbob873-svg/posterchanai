"""COUNTS over the relay's store, computed INSIDE the relay process and asked for over its control dir (#161).

Server Stats, the NostrStats bot and the admin's relay trace used to open the relay's Postgres from the app and
run their own SQL. Retiring Postgres as the relay's store takes that door away, and pulling millions of events
over a websocket to count them is not a replacement. So the relay counts, from whatever it is serving from:

  * PosterChanDB when the mirror is SERVING -- numpy over COPIES of its per-event columns, taken in short pieces
    under the store lock and counted after it is released (`Store.stats_columns`); a query thread never waits
    behind a statistic (one 465 ms read under that lock starved the relay, 2026-10-09);
  * Postgres otherwise -- the same SQL as before, on the relay's OWN connection, on a dedicated single
    "relay-stats" thread: the four read workers that answer clients are never borrowed.

Every aggregate is defined ONCE here as a composition of a few primitives (`_Sql` and `_Pcdb` implement the same
primitives), so the two backends cannot drift in what a number means; tests/test_relay_aggregates.py runs both
over the same events.

The IPC is the control dir the app already uses for admin commands, plus an answer: the app writes
`ask_<rid>.json`, the relay's ask poller (0.5 s) computes on the stats thread and writes `answers/<rid>.json`.
`ask()` raises relay_reader.Unavailable when the relay is down, refuses, or does not answer in time -- "could not
ask" is never an empty answer, and every caller shows it as unknown, never as zero.

Answers carry COUNTS, pubkeys and timestamps only -- never content (the trace's recent notes are read by the app
over an ordinary REQ, as any client would).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# --- what the stats mean: defined in stats_service (import-light: stdlib only), counted here ------------------
from app.services.stats_service import (_ALL_KINDS, GAME_PREFIXES, WINDOWS, _LOCAL,  # noqa: E402
                                        _ONE_TIME_KINDS, _PERSON_PUBKEY, CHAT_PREFIX, MONERO_TIP)

DAY = 86400


# --- primitives: Postgres (and sqlite3 in tests -- `?` placeholders, portable SQL only) ------------------------

class _Sql:
    backend = "postgres"

    def __init__(self, conn, size_sql: str | None = "SELECT pg_database_size(current_database())"):
        self.conn = conn
        self.size_sql = size_sql

    def _rows(self, sql, params=()):
        return [tuple(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def kind_buckets(self, start, end, step, kinds):
        """[(bucket, kind, n)] of locally-published events of `kinds` in [start, end)."""
        return self._rows("SELECT (created_at / ?) * ? AS bucket, kind, count(*) FROM events "
                          "WHERE created_at >= ? AND created_at < ? AND kind IN (%s) AND %s GROUP BY 1, 2"
                          % (",".join("?" * len(kinds)), _LOCAL), (step, step, start, end, *kinds))

    def tag_buckets(self, start, end, step, kind, tag, value):
        """[(bucket, n)] of locally-published `kind` events carrying tag=value -- each event once, however many
        times its publisher repeated the tag."""
        return self._rows("SELECT (e.created_at / ?) * ? AS bucket, count(*) FROM events e "
                          "WHERE e.created_at >= ? AND e.created_at < ? AND e.kind = ? AND e.origin = 'direct' "
                          "AND EXISTS (SELECT 1 FROM event_tags t WHERE t.event_id = e.id AND t.tag = ? AND t.value = ?) "
                          "GROUP BY 1", (step, step, start, end, kind, tag, value))

    def window_totals(self, start, end):
        """(events, people) published here in [start, end)."""
        r = self._rows("SELECT count(*), count(DISTINCT " + _PERSON_PUBKEY + ") FROM events "
                       "WHERE created_at >= ? AND created_at < ? AND " + _LOCAL, (start, end))[0]
        return int(r[0] or 0), int(r[1] or 0)

    def d_distinct(self, prefixes: dict, start=None, end=None) -> dict:
        """{name: distinct d values starting with prefixes[name]} -- any origin; in [start, end) when given."""
        out = {name: 0 for name in prefixes}
        if start is None:
            for name, pre in prefixes.items():
                out[name] = int(self._rows("SELECT count(DISTINCT value) FROM event_tags WHERE tag = 'd' AND value LIKE ?",
                                           (pre + "%",))[0][0] or 0)
            return out
        cases, wheres, cp, wp = [], [], [], []
        for name, pre in prefixes.items():
            cases.append("WHEN t.value LIKE ? THEN ?")
            cp += [pre + "%", name]
            wheres.append("t.value LIKE ?")
            wp.append(pre + "%")
        rows = self._rows("SELECT CASE %s END AS g, count(DISTINCT t.value) "
                          "FROM event_tags t JOIN events e ON e.id = t.event_id "
                          "WHERE t.tag = 'd' AND (%s) AND e.created_at >= ? AND e.created_at < ? GROUP BY 1"
                          % (" ".join(cases), " OR ".join(wheres)), (*cp, *wp, start, end))
        for name, n in rows:
            if name in out:
                out[name] = int(n or 0)
        return out

    def origins(self, day_since) -> dict:
        """{origin: {"total": n, "day": n}} over EVERY stored event (the relay panel's store breakdown)."""
        rows = self._rows("SELECT origin, count(*), sum(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) "
                          "FROM events GROUP BY 1", (day_since,))
        return {str(o or "?"): {"total": int(t or 0), "day": int(d or 0)} for o, t, d in rows}

    def kind_count(self, kind) -> int:
        return int(self._rows("SELECT count(*) FROM events WHERE kind = ? AND " + _LOCAL, (kind,))[0][0] or 0)

    def people_since(self, since) -> int:
        return int(self._rows("SELECT count(DISTINCT " + _PERSON_PUBKEY + ") FROM events WHERE created_at >= ? AND "
                              + _LOCAL, (since,))[0][0] or 0)

    def d_count(self, prefix, since=None) -> int:
        """How many `d` tags start with `prefix` (any origin; on events created since `since` when given)."""
        if since is None:
            return int(self._rows("SELECT count(*) FROM event_tags WHERE tag = 'd' AND value LIKE ?",
                                  (prefix + "%",))[0][0] or 0)
        return int(self._rows("SELECT count(*) FROM event_tags t JOIN events e ON e.id = t.event_id "
                              "WHERE t.tag = 'd' AND t.value LIKE ? AND e.created_at >= ?",
                              (prefix + "%", since))[0][0] or 0)

    def d_daily(self, prefix, since) -> list:
        """[(UTC day start, n)] of `d` tags starting with `prefix` on events created since `since`."""
        return self._rows("SELECT (e.created_at / %d) * %d AS d, count(*) FROM event_tags t "
                          "JOIN events e ON e.id = t.event_id WHERE t.tag = 'd' AND t.value LIKE ? "
                          "AND e.created_at >= ? GROUP BY 1" % (DAY, DAY), (prefix + "%", since))

    def db_bytes(self):
        if not self.size_sql:
            return None
        return int(self._rows(self.size_sql)[0][0] or 0)

    def nip05_holders(self) -> set:
        """Authors whose stored kind-0 carries a non-empty `nip05` (the relay keeps one kind-0 per author)."""
        out = set()
        for pk, content in self._rows("SELECT pubkey, content FROM events WHERE kind = 0 AND content LIKE ?",
                                      ("%nip05%",)):
            if _has_nip05(content):
                out.add(pk)
        return out

    def daily_author_counts(self, kind, since, authors: set) -> list:
        """[(UTC day start, pubkey, n)] for `kind` events by `authors` created since `since`."""
        rows = self.conn.execute("SELECT (created_at / %d) * %d AS d, pubkey, count(*) FROM events "
                                 "WHERE kind = ? AND created_at >= ? GROUP BY 1, 2" % (DAY, DAY), (kind, since))
        out = []
        while True:
            chunk = rows.fetchmany(5000)
            if not chunk:
                break
            out.extend((int(d), pk, int(n)) for d, pk, n in (tuple(r) for r in chunk) if pk in authors)
        return out

    def author_groups(self, pubkey) -> list:
        """[(kind, origin, n, first, last)] of everything stored for `pubkey`."""
        return self._rows("SELECT kind, origin, count(*), min(created_at), max(created_at) FROM events "
                          "WHERE pubkey = ? GROUP BY kind, origin", (pubkey,))


# --- primitives: PosterChanDB ----------------------------------------------------------------------------------

class _Pcdb:
    backend = "posterchandb"

    def __init__(self, store, now: int):
        self.st, self.now = store, int(now)
        self._cols = None
        self._pref: dict = {}

    def _c(self):
        """The column copies, taken once per answer and only when an aggregate needs them."""
        if self._cols is None:
            import numpy as np
            c = self.st.stats_columns()
            created = c["created"].astype(np.int64)
            exp = c["expires"]
            live = (c["dead"] == 0) & ((exp == 0) | (exp > self.now))
            self._cols = {"n": c["n"], "created": created, "kind": c["kind"], "origin": c["origin"],
                          "author": c["author"], "authors": c["authors"], "live": live,
                          "direct": live & (c["origin"] == 0)}
        return self._cols

    @staticmethod
    def _within(seqs, n):
        return seqs[seqs < n]

    def _win(self, start, end):
        c = self._c()["created"]
        return (c >= start) & (c < end)

    def kind_buckets(self, start, end, step, kinds):
        import numpy as np
        c = self._c()
        m = c["direct"] & self._win(start, end) & np.isin(c["kind"], np.asarray(kinds, dtype=np.uint32))
        if not m.any():
            return []
        pairs = np.stack([(c["created"][m] // step) * step, c["kind"][m].astype(np.int64)], axis=1)
        keys, counts = np.unique(pairs, axis=0, return_counts=True)
        return [(int(b), int(k), int(n)) for (b, k), n in zip(keys, counts)]

    def tag_buckets(self, start, end, step, kind, tag, value):
        import numpy as np
        c = self._c()
        s = self._within(self.st.tag_seqs(tag, value), c["n"])
        if not len(s):
            return []
        cr = c["created"][s]
        m = c["direct"][s] & (c["kind"][s] == kind) & (cr >= start) & (cr < end)
        keys, counts = np.unique((cr[m] // step) * step, return_counts=True)
        return [(int(b), int(n)) for b, n in zip(keys, counts)]

    def window_totals(self, start, end):
        import numpy as np
        c = self._c()
        m = c["direct"] & self._win(start, end)
        people = m & ~np.isin(c["kind"], np.asarray(_ONE_TIME_KINDS, dtype=np.uint32))
        return int(m.sum()), int(len(np.unique(c["author"][people])))

    def _pairs(self, prefix, start=None, end=None):
        """(values, seqs) of `d` tags starting with `prefix` on live events (created in [start, end) when given).
        The prefix's postings are read once per answer: Server Stats asks about the same prefixes several times."""
        import numpy as np
        c = self._c()
        got = self._pref.get(prefix)
        if got is None:
            pairs = self.st.d_prefix_pairs(prefix)
            seqs = np.fromiter((q for _, q in pairs), dtype=np.int64, count=len(pairs))
            vals = np.array([v for v, _ in pairs], dtype=object)
            keep = seqs < c["n"]
            seqs, vals = seqs[keep], vals[keep]
            keep = c["live"][seqs]
            got = self._pref[prefix] = (vals[keep], seqs[keep])
        vals, seqs = got
        if start is not None:
            cr = c["created"][seqs]
            m = (cr >= start) & (cr < end)
            vals, seqs = vals[m], seqs[m]
        return vals, seqs

    def d_distinct(self, prefixes: dict, start=None, end=None) -> dict:
        return {name: len(set(self._pairs(pre, start, end)[0].tolist())) for name, pre in prefixes.items()}

    def origins(self, day_since) -> dict:
        import numpy as np
        from app.services.posterchandb.store import ORIGIN_NAMES
        c = self._c()
        live = c["live"]
        o = c["origin"][live]
        recent = c["created"][live] >= day_since
        out = {}
        for code in np.unique(o):
            m = o == code
            out[ORIGIN_NAMES.get(int(code), "?")] = {"total": int(m.sum()), "day": int((m & recent).sum())}
        return out

    def kind_count(self, kind) -> int:
        c = self._c()
        return int((c["direct"] & (c["kind"] == kind)).sum())

    def people_since(self, since) -> int:
        import numpy as np
        c = self._c()
        m = c["direct"] & (c["created"] >= since) & ~np.isin(c["kind"], np.asarray(_ONE_TIME_KINDS, dtype=np.uint32))
        return int(len(np.unique(c["author"][m])))

    def d_count(self, prefix, since=None) -> int:
        return int(len(self._pairs(prefix, since, 1 << 62)[1] if since is not None else self._pairs(prefix)[1]))

    def d_daily(self, prefix, since) -> list:
        import numpy as np
        seqs = self._pairs(prefix, since, 1 << 62)[1]
        keys, counts = np.unique(self._c()["created"][seqs] // DAY * DAY, return_counts=True)
        return [(int(d), int(n)) for d, n in zip(keys, counts)]

    def db_bytes(self):
        return int(self.st.disk_bytes())

    def nip05_holders(self) -> set:
        return {pk for pk, content in self.st.contents(self.st.kind_seqs(0)) if _has_nip05(content)}

    def daily_author_counts(self, kind, since, authors: set) -> list:
        import numpy as np
        c = self._c()
        ids = [i for i, pk in enumerate(c["authors"]) if pk in authors]
        if not ids:
            return []
        m = c["live"] & (c["kind"] == kind) & (c["created"] >= since) & np.isin(c["author"], np.asarray(ids, dtype=np.uint32))
        if not m.any():
            return []
        pairs = np.stack([c["created"][m] // DAY * DAY, c["author"][m].astype(np.int64)], axis=1)
        keys, counts = np.unique(pairs, axis=0, return_counts=True)
        names = c["authors"]
        return [(int(d), names[int(a)], int(n)) for (d, a), n in zip(keys, counts)]

    def author_groups(self, pubkey) -> list:
        """Per-author, so it reads only that author's rows (no column copy)."""
        seqs = self.st.author_seqs(pubkey)
        rows = self.st.columns_for(seqs)
        from app.services.posterchandb.store import ORIGIN_NAMES
        g: dict = {}
        for kind, origin, created, dead, exp in rows:
            if dead or (exp and exp <= self.now):
                continue
            key = (int(kind), ORIGIN_NAMES.get(int(origin), "?"))
            n, lo, hi = g.get(key, (0, None, None))
            g[key] = (n + 1, created if lo is None else min(lo, created), created if hi is None else max(hi, created))
        return [(k, o, n, lo, hi) for (k, o), (n, lo, hi) in sorted(g.items())]


def _has_nip05(content) -> bool:
    try:
        m = json.loads(content or "{}")
        return isinstance(m, dict) and bool(str(m.get("nip05", "") or "").strip())
    except Exception:       # noqa: BLE001 -- somebody's malformed profile is not a stat
        return False


# --- the answers ----------------------------------------------------------------------------------------------

def _part(fn, *a):
    """One figure. A part that cannot be computed is None ("unknown"), never 0, and never takes the rest down."""
    try:
        return fn(*a)
    except Exception as e:      # noqa: BLE001
        logger.warning("[relay-stats] %s failed: %s", getattr(fn, "__name__", fn), e)
        return None


def server_stats(src, now: int) -> dict:
    """Every relay-derived number on Server Stats, raw (stats_service aligns the buckets)."""
    windows = {}
    for key, span, step in WINDOWS:
        start = now - span
        tot = _part(src.window_totals, start, now)
        windows[key] = {"kinds": _part(src.kind_buckets, start, now, step, _ALL_KINDS),
                        "monero": _part(src.tag_buckets, start, now, step, 1, *MONERO_TIP),
                        "events": tot[0] if tot else None, "people": tot[1] if tot else None,
                        "by_game": _part(src.d_distinct, GAME_PREFIXES, start, now)}
    return {
        "backend": src.backend, "now": now, "windows": windows,
        "games": _part(src.d_distinct, GAME_PREFIXES),
        "origins": _part(src.origins, now - DAY),
        "notes": _part(src.kind_count, 1),
        "streams": _part(src.kind_count, 30311),
        "profiles": _part(src.kind_count, 0),
        "pubkeys_24h": _part(src.people_since, now - DAY),
        "pubkeys_30d": _part(src.people_since, now - 30 * DAY),
        "ai_requests": _part(src.d_count, CHAT_PREFIX),
        "ai_requests_24h": _part(src.d_count, CHAT_PREFIX, now - DAY),
        "chat_daily": _part(src.d_daily, CHAT_PREFIX, now - 30 * DAY),
        "db_bytes": _part(src.db_bytes),
    }


def nip05_activity(src, since: int, exclude=()) -> dict:
    """The stats bot's input: who holds a NIP-05 (minus bridge puppets) and their kind-1 notes per UTC day since
    `since`, as [day, author index, n] rows. Raises when it cannot be computed -- the bot must not post zeros."""
    holders = src.nip05_holders() - set(exclude or ())
    authors = sorted(holders)
    ix = {pk: i for i, pk in enumerate(authors)}
    rows = src.daily_author_counts(1, int(since), holders)
    return {"backend": src.backend, "nip05": authors, "activity": [[d, ix[pk], n] for d, pk, n in rows]}


def trace(store, src, pubkey: str) -> dict:
    """The relay-trace facts that only the relay holds: what it stored per kind/origin, the WoT row, the tier."""
    from .wot import WOT_TIERS_KEY
    wot = store._wot_row_sync(pubkey)
    tier_row = None
    raw = store._kv_get_sync(WOT_TIERS_KEY)
    if raw:
        rec = json.loads(raw)
        tier_row = [(rec.get("tiers") or {}).get(pubkey), rec.get("built_at"), rec.get("depth"),
                    rec.get("min_followers")]
    return {"groups": [list(g) for g in src.author_groups(pubkey)], "wot": list(wot) if wot else None,
            "tier_row": tier_row}


def wot_tiers(store, pubkeys: list) -> dict:
    """{pubkey: [tier, vouchers]} for the members among `pubkeys`, None for members the last rebuild did not
    record (operators, admitted between rebuilds). Non-members are absent."""
    from .wot import WOT_TIERS_KEY
    members = store._wot_among_sync(list(pubkeys or []))
    out = {m: None for m in members}
    if members:
        raw = store._kv_get_sync(WOT_TIERS_KEY)
        tiers = (json.loads(raw).get("tiers") or {}) if raw else {}
        for m in members:
            if isinstance(tiers.get(m), list):
                out[m] = tiers[m]
    return out


# --- the relay side: the ask poller (runs in the relay process) -------------------------------------------------

_EXEC: ThreadPoolExecutor | None = None
_CACHE: dict = {}                       # key -> (expires monotonic, result)
_CACHE_TTL = {"server-stats": 50.0, "nip05-activity": 300.0}
_RID = re.compile(r"^[0-9a-f]{8,64}$")
ASK_MAX_AGE = 120                       # a request older than this was given up on by its asker
ANSWER_MAX_AGE = 300


def _executor() -> ThreadPoolExecutor:
    global _EXEC
    if _EXEC is None:
        # ONE thread, its own: a statistic never takes a read worker from a client, and two never run at once.
        _EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="relay-stats")
    return _EXEC


def source(store, now: int):
    """PosterChanDB when the mirror is serving, else Postgres on THIS thread's own connection."""
    m = getattr(store, "mirror", None)
    try:
        if m is not None and m.serving() and m.store is not None:
            return _Pcdb(m.store, now)
    except Exception:       # noqa: BLE001
        pass
    return _Sql(store._conn())


def compute(store, what: str, args: dict, now: int | None = None) -> dict:
    """BLOCKING (the stats thread): the answer to one ask."""
    now = int(now or time.time())
    args = args or {}
    key = json.dumps([what, args], sort_keys=True)
    hit = _CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    src = source(store, now)
    if what == "server-stats":
        out = server_stats(src, now)
    elif what == "nip05-activity":
        exclude = set()
        try:
            exclude = set(store._bridge_puppets_all_sync())
        except Exception:       # noqa: BLE001 -- no puppet registry: count every holder, as before
            pass
        out = nip05_activity(src, int(args.get("since") or 0), exclude)
    elif what == "trace":
        pk = str(args.get("pubkey") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", pk):
            raise ValueError("bad pubkey")
        out = trace(store, src, pk)
    elif what == "wot-tiers":
        pks = [str(p).lower() for p in (args.get("pubkeys") or [])][:2000]
        out = wot_tiers(store, pks)
    else:
        raise ValueError("unknown ask %r" % (what,))
    ttl = _CACHE_TTL.get(what)
    if ttl:
        _CACHE[key] = (time.monotonic() + ttl, out)
        for k in [k for k, v in _CACHE.items() if v[0] <= time.monotonic()]:
            _CACHE.pop(k, None)
    return out


def _write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def answer_one(store, control_dir: str, req: dict) -> None:
    """BLOCKING: compute one request and write its answer file. Never raises."""
    rid = str(req.get("rid") or "")
    if not _RID.match(rid):
        return
    adir = os.path.join(control_dir, "answers")
    t0 = time.monotonic()
    try:
        res = {"ok": True, "result": compute(store, str(req.get("what") or ""), req.get("args") or {})}
    except Exception as e:      # noqa: BLE001
        res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    res["ms"] = int((time.monotonic() - t0) * 1000)
    try:
        os.makedirs(adir, exist_ok=True)
        _write_json(os.path.join(adir, rid + ".json"), res)
    except Exception as e:      # noqa: BLE001
        logger.warning("[relay-stats] could not write an answer: %s", e)
    if res["ms"] > 2000:
        logger.info("[relay-stats] %s took %d ms", req.get("what"), res["ms"])


def take_requests(control_dir: str, now: float | None = None) -> list:
    """Read + remove every pending ask (fresh ones only), and sweep answers nobody collected."""
    now = time.time() if now is None else now
    out = []
    for path in sorted(glob.glob(os.path.join(control_dir, "ask_*.json"))):
        try:
            with open(path) as f:
                req = json.load(f)
        except Exception:       # noqa: BLE001
            req = None
        try:
            os.remove(path)
        except Exception:       # noqa: BLE001
            pass
        if isinstance(req, dict) and now - float(req.get("ts") or 0) <= ASK_MAX_AGE:
            out.append(req)
    for path in glob.glob(os.path.join(control_dir, "answers", "*.json")):
        try:
            if now - os.path.getmtime(path) > ANSWER_MAX_AGE:
                os.remove(path)
        except Exception:       # noqa: BLE001
            pass
    return out


async def ask_poller(store, control_dir: str, stop_event, interval: float = 0.5) -> None:
    """The relay's half: drain ask_*.json and answer each on the stats thread. Identical concurrent asks are
    answered by one computation (the cache serves the rest the moment it lands)."""
    import asyncio
    loop = asyncio.get_running_loop()
    os.makedirs(control_dir, exist_ok=True)
    while not stop_event.is_set():
        try:
            for req in take_requests(control_dir):
                loop.run_in_executor(_executor(), answer_one, store, control_dir, req)
        except Exception as e:      # noqa: BLE001
            logger.debug("[relay-stats] ask poll error: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# --- the asking side (the app, the worker) ----------------------------------------------------------------------

def ask(what: str, args: dict | None = None, *, timeout: float = 30.0, control_dir: str | None = None,
        check_running: bool = True) -> dict:
    """BLOCKING: ask the relay process for one aggregate. Raises relay_reader.Unavailable when it cannot be asked
    (not running, refused, or no answer within `timeout`) -- never an empty answer."""
    from app.services.relay_reader import Unavailable
    from . import thread as _thread
    if check_running and not _thread.relay_status().get("running"):
        raise Unavailable("the relay is not running")
    ctrl = control_dir or _thread._relay_paths(_thread._relay_db_path())["control"]
    rid = uuid.uuid4().hex
    req_path = os.path.join(ctrl, "ask_%s.json" % rid)
    ans_path = os.path.join(ctrl, "answers", rid + ".json")
    try:
        os.makedirs(ctrl, exist_ok=True)
        _write_json(req_path, {"what": what, "args": args or {}, "rid": rid, "ts": time.time()})
    except Exception as e:      # noqa: BLE001
        raise Unavailable("could not hand the relay a request: %s" % e) from e
    end = time.monotonic() + timeout
    delay = 0.05
    while time.monotonic() < end:
        if os.path.exists(ans_path):
            try:
                with open(ans_path) as f:
                    res = json.load(f)
            except Exception as e:      # noqa: BLE001
                raise Unavailable("unreadable relay answer: %s" % e) from e
            finally:
                try:
                    os.remove(ans_path)
                except Exception:       # noqa: BLE001
                    pass
            if not res.get("ok"):
                raise Unavailable("the relay could not answer: %s" % res.get("error"))
            return res.get("result")
        time.sleep(delay)
        delay = min(0.25, delay * 1.5)
    try:
        os.remove(req_path)            # never picked up: nobody should compute it for nobody
    except Exception:       # noqa: BLE001
        pass
    raise Unavailable("the relay did not answer %r within %.0fs" % (what, timeout))

