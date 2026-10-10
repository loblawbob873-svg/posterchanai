"""The global feed -- `{"kinds":[1,6],"limit":200}`, what every client asks first -- is fast at real size.

2026-10-09, serve mode on server1: the planner turned a filter with nothing selective into the union of every
kind-1 and kind-6 posting (over a million at 2.6M events) and sorted it -- 465 ms under the store's one lock --
and a few clients refreshing their feeds queued everybody else behind them until requests timed out. It now
walks newest-first and stops once the newest `limit` are certain (store._newest_first). This builds a store
large enough for the old plan to be slow, checks the answers against a brute-force scan, and times the shapes
clients send while three other threads ask the same.
"""
import hashlib
import random
import threading
import time

from app.services.posterchandb.store import Store

N = 300_000


def _ev(i, r, now):
    kind = r.choice([1, 1, 1, 6, 7, 7, 0, 3, 30023, 1111])
    created = now - r.randint(0, 120 * 86400)
    eid = hashlib.sha256(b"%d" % i).hexdigest()
    return {"id": eid, "pubkey": "%064x" % r.randrange(1, 4000), "created_at": created, "kind": kind,
            "tags": [], "content": "", "sig": "0" * 128}


def _brute(st, flt, now):
    """ORDER BY created_at DESC, id DESC over everything, with the same filters -- no planner involved."""
    n = len(st.off)
    rows = []
    for s in range(n):
        if st.dead[s]:
            continue
        if flt.get("kinds") and st.kind[s] not in flt["kinds"]:
            continue
        c = st.created[s]
        if flt.get("since") is not None and c < flt["since"]:
            continue
        if flt.get("until") is not None and c > flt["until"]:
            continue
        rows.append((c, st._id_hex(s)))
    rows.sort(reverse=True)
    return [i for _, i in rows[:max(1, min(int(flt.get("limit") or 500), 5000))]]


def test_the_global_feed_is_exact_and_fast_at_scale_under_concurrency(tmp_path):
    r, now = random.Random(7), int(time.time())
    st = Store(str(tmp_path / "db"), flush_interval=3600, direct_durable=False)
    # copied oldest-first like the mirror's copy, then a stretch of live arrivals out of order
    evs = sorted((_ev(i, r, now) for i in range(N)), key=lambda e: (e["created_at"], e["id"]))
    for e in evs:
        st.copy_put(e, origin="wot")
    for i in range(N, N + 2000):
        st.copy_put(_ev(i, r, now), origin="wot")
    shapes = [{"kinds": [1, 6], "limit": 200}, {"kinds": [1], "limit": 50}, {"limit": 100},
              {"kinds": [1], "since": now - 3600, "limit": 500}, {"kinds": [1, 6], "until": now - 30 * 86400, "limit": 100},
              {"kinds": [30023], "limit": 50}]
    try:
        for f in shapes:
            assert [e["id"] for e in st.query(dict(f), now=now)] == _brute(st, f, now), f
        lat, stop = [], threading.Event()

        def ask():
            while not stop.is_set():
                t = time.perf_counter()
                st.query({"kinds": [1, 6], "limit": 200}, now=now)
                lat.append(time.perf_counter() - t)
        threads = [threading.Thread(target=ask, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(2.0)
        stop.set()
        for t in threads:
            t.join(5)
        lat.sort()
        p95 = lat[int(len(lat) * 0.95)]
        assert p95 < 0.05, "global feed p95 %.0f ms with 4 clients asking: the relay would queue" % (p95 * 1000)
    finally:
        st.close()
