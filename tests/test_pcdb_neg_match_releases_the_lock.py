"""A negentropy / COUNT match over a long `authors` list never holds the store lock for the whole list.

2026-10-18, py-spy on server1 during the relay's 70-130% CPU spikes: a reader thread inside `store.match` ->
`_match_locked` -> `_union`, the EXHAUSTIVE match NEG-OPEN and COUNT use. A PosterChanOS desktop syncing "my follows'
last week" names ~1000 authors; the union of their posting lists was built and sorted under `self._lock`, the one
store-wide lock, so every query and every write on the relay waited behind it. Now the authors are matched a chunk
per hold of the lock, and the answer is the same set, ascending, dead and expired events never in it.
"""
import random
import threading
import time

import numpy as np

from app.services.posterchandb import store as S
from app.services.posterchandb.store import Store
from tests.test_posterchandb import mk

BASE = 1_700_000_000
EXPIRES = int(time.time()) + 86400 * 200     # stored (not yet expired at put) ...
NOW = int(time.time()) + 86400 * 400         # ... and expired at the `now` the matches are asked at


def _store(tmp_path, n_authors=300, n_events=6000, seed=4):
    st = Store(str(tmp_path), flush_interval=3600, direct_durable=False)
    r = random.Random(seed)
    authors = ["%064x" % (i + 11) for i in range(n_authors)]
    evs = []
    for i in range(n_events):
        e = mk(kind=r.choice([0, 1, 6, 7, 1111]), pubkey=r.choice(authors), created_at=BASE + r.randrange(86400 * 30),
               content="n%d" % i)
        if i % 97 == 0:
            e["tags"] = [["expiration", str(EXPIRES)]]
        st.put(e, origin="wot")
        evs.append(e)
        if i == n_events // 2:
            st.flush()                                              # postings in more than one run
    st.kill([s for s in (st.seq_of(e["id"]) for e in evs[::53]) if s is not None])   # auto-cleaned
    return st, authors, evs


def _want(st, evs, flt, now):
    dead = {e["id"] for e in evs if st.seq_of(e["id"]) is None or st.dead[st.seq_of(e["id"])]}
    out = []
    for e in evs:
        if e["id"] in dead or e["pubkey"] not in flt["authors"]:
            continue
        if "kinds" in flt and e["kind"] not in flt["kinds"]:
            continue
        if any(t[0] == "expiration" and int(t[1]) <= now for t in e["tags"]):
            continue
        if e["created_at"] < flt.get("since", 0) or e["created_at"] > flt.get("until", 1 << 62):
            continue
        out.append(st.seq_of(e["id"]))
    return sorted(out)


def test_a_long_authors_match_is_the_same_answer(tmp_path):
    st, authors, evs = _store(tmp_path)
    now = NOW
    r = random.Random(1)
    follows = r.sample(authors, 250) + ["%064x" % 999_999] + authors[:5]      # a stranger, and repeats
    for flt in ({"authors": follows, "kinds": [1, 6, 7], "since": BASE + 86400 * 23},
                {"authors": follows, "since": BASE + 86400 * 23},
                {"authors": follows, "kinds": [1, 6, 7]},
                {"authors": follows},
                {"authors": follows, "kinds": [0, 1111], "since": BASE + 86400 * 3, "until": BASE + 86400 * 9}):
        got = st.match(dict(flt), now=now)
        assert got.dtype == np.uint32
        assert [int(x) for x in got] == _want(st, evs, flt, now), flt
    st.close()


class _CountingLock:
    """The store's RLock, counting OUTERMOST holds (a hold = the lock taken by nobody before)."""

    def __init__(self, real):
        self.real, self.n, self.depth = real, 0, 0

    def __enter__(self):
        self.real.acquire()
        if self.depth == 0:
            self.n += 1
        self.depth += 1
        return self

    def __exit__(self, *a):
        self.depth -= 1
        self.real.release()

    def acquire(self, *a, **k):
        return self.real.acquire(*a, **k)

    def release(self):
        return self.real.release()


def test_the_lock_is_released_between_author_chunks(tmp_path, monkeypatch):
    """The posting lists of ~1000 followed authors are never all read in one hold of the store lock."""
    st, authors, evs = _store(tmp_path, n_authors=1000, n_events=4000)
    monkeypatch.setattr(S, "_MATCH_KEYS_PER_HOLD", 64)
    lock = _CountingLock(st._lock)
    st._lock = lock
    per_hold = {}
    real_get_many = st.idx.get_many

    def spy(keys):
        keys = list(keys)
        per_hold[lock.n] = per_hold.get(lock.n, 0) + len(keys)
        return real_get_many(keys)
    monkeypatch.setattr(st.idx, "get_many", spy)
    flt = {"authors": authors, "kinds": [1, 6, 7], "since": BASE + 86400 * 20}
    got = st.match(dict(flt), now=NOW)
    st._lock = lock.real
    assert [int(x) for x in got] == _want(st, evs, flt, NOW)
    assert len(per_hold) >= 3000 // 64, per_hold
    assert max(per_hold.values()) <= 64, "one hold of the store lock read %d posting lists" % max(per_hold.values())
    st.close()


def test_one_heavy_authors_history_is_not_read_in_one_hold(tmp_path, monkeypatch):
    """Chunking by author is not enough: one author can hold most of the store. The columns are checked a bounded
    number of postings per hold."""
    st = Store(str(tmp_path), flush_interval=3600, direct_durable=False)
    pk = "%064x" % 77
    evs = [mk(kind=1, pubkey=pk, created_at=BASE + i, content="h%d" % i) for i in range(3000)]
    for e in evs:
        st.put(e, origin="wot")
    monkeypatch.setattr(S, "_MATCH_ROWS_PER_HOLD", 250)
    lock = _CountingLock(st._lock)
    st._lock = lock
    got = st.match({"authors": [pk], "since": BASE + 100}, now=NOW)
    st._lock = lock.real
    assert len(got) == 2900
    assert lock.n >= 3000 // 250, "a 3000-event history was checked in %d holds of the lock" % lock.n
    st.close()


def test_a_writer_gets_in_while_a_big_match_runs(tmp_path, monkeypatch):
    """The behaviour the chunking is for: a write lands DURING the match, not after it."""
    st, authors, evs = _store(tmp_path, n_authors=1000, n_events=4000)
    started, landed_during = threading.Event(), []
    stop = threading.Event()
    sizes = []
    real_get_many = st.idx.get_many

    def spy(keys):
        sizes.append(len(st.off))                 # the store's size as each chunk is read
        if len(sizes) == 1:
            started.set()
        return real_get_many(keys)
    monkeypatch.setattr(st.idx, "get_many", spy)

    def writer():
        started.wait(5)
        i = 0
        while not stop.is_set() and i < 2000:
            st.put(mk(kind=1, pubkey=authors[0], created_at=BASE + 5, content="w%d" % i), origin="wot")
            i += 1
    th = threading.Thread(target=writer)
    th.start()
    for _ in range(20):
        st.match({"authors": authors, "kinds": [1, 6, 7]}, now=NOW)
        landed_during.append(len(set(sizes)) > 1)
        sizes.clear()
        if any(landed_during):
            break
    stop.set()
    th.join()
    assert any(landed_during), "no write landed while a 1000-author match ran"
    st.close()


def test_a_socket_over_its_negentropy_cap_is_refused_before_the_item_set_is_built():
    """Building the item set IS the cost. A socket already holding its full share of NEG sessions used to have its
    whole exhaustive match built and then be told "rate-limited"; now nothing is asked of the store. A re-opened
    sub_id replaces its own session and is still served."""
    import asyncio

    from app.services.nostr_relay import negentropy
    from app.services.nostr_relay.server import RelayServer

    class CountingStore:
        def __init__(self):
            self.calls = 0

        async def neg_items(self, filters, cap=0):
            self.calls += 1
            return []

    class Gate:
        def is_member(self, _pk): return True
        def is_operator(self, _pk): return False
        def is_blocked(self, _pk): return False
        def is_puppet_event(self, _ev): return False

    store = CountingStore()
    srv = RelayServer(store, Gate(), {"wot_enabled": False, "max_subs_per_conn": 2})
    sent = []
    srv._send = lambda conn, obj: sent.append(obj)
    conn = object()
    first = negentropy.Initiator([]).initiate().hex()
    flt = {"kinds": [1], "authors": ["a" * 64]}
    for sub in ("n1", "n2"):
        asyncio.run(srv._on_neg_open(conn, sub, dict(flt), first))
    assert store.calls == 2 and [m[0] for m in sent] == ["NEG-MSG", "NEG-MSG"]
    asyncio.run(srv._on_neg_open(conn, "n3", dict(flt), first))
    assert sent[-1] == ["NEG-ERR", "n3", "rate-limited"]
    assert store.calls == 2, "the item set was built for a socket that was then refused"
    asyncio.run(srv._on_neg_open(conn, "n1", dict(flt), first))
    assert sent[-1][0] == "NEG-MSG" and store.calls == 3
