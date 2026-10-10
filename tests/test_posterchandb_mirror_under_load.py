"""The relay answering from PosterChanDB stays fast while the firehose writes -- the night it did not.

2026-10-09, serve mode on server1: queries timed out for clients (desktop "not reconnecting"), 1,970 of ~12,000
fell back. Two causes, both invisible to every single-threaded test:
  * a query WAITED up to 0.5 s for the mirror to apply every queued write, and under a constant firehose the
    mirror is always a little behind -- so almost every query waited, and the relay's few query threads were
    all parked waiting;
  * the mirror fell behind because each live write ran the Postgres-exact search tokenizer (~93% of storing an
    event) while the background word indexer competed for the same GIL.
Here a writer streams events through the mirror while a reader queries it, with the word backlog of a fresh
copy pending: queries must stay fast (no query may sit in a wait), the mirror must keep up, and the answers
served must be exact.
"""
import threading
import time

from app.services.posterchandb import mirror as M
from app.services.posterchandb.store import Store
from tests.test_posterchandb_durability import _events


def test_queries_stay_fast_and_served_while_the_firehose_writes(tmp_path):
    st = Store(str(tmp_path / "db"), flush_interval=3600, direct_durable=False)
    evs = _events(51, 26000)
    for ev in evs[:20000]:
        st.copy_put(dict(ev), origin="wot")               # a fresh copy: 20k search words still pending
    holds = []                                            # how long each word batch holds the store lock
    real_index = st.index_pending_words

    def timed_index(limit):
        t = time.monotonic()
        try:
            return real_index(limit)
        finally:
            holds.append(time.monotonic() - t)
    st.index_pending_words = timed_index
    m = M.Mirror(str(tmp_path / "m"), "serve", lambda: None, maintenance=False)
    m.store, m.state = st, "ready"
    m._set("ready", "test")                               # starts the background word indexer, as in the relay
    applier = threading.Thread(target=m._apply_loop, daemon=True)
    applier.start()
    stop = threading.Event()

    def firehose():
        for ev in evs[20000:]:                             # ~2,000 writes a second for ~3 s
            if stop.is_set():
                return
            m.put(dict(ev), "wot")
            time.sleep(0.0005)
    w = threading.Thread(target=firehose, daemon=True)
    w.start()
    lat = []
    t_end = time.monotonic() + 3.0
    while time.monotonic() < t_end:
        t = time.monotonic()
        m.query([{"kinds": [1, 7], "limit": 50}], 5000)
        lat.append(time.monotonic() - t)
        time.sleep(0.005)
    stop.set()
    w.join(10)
    m._stop.set()
    applier.join(10)
    lat.sort()
    p95 = lat[int(len(lat) * 0.95)]
    served, fell = m.counters["served"], m.counters["fallback"]
    try:
        assert p95 < 0.05, "p95 query %.0f ms under load: the relay's query threads would park" % (p95 * 1000)
        holds.sort()
        hold = holds[len(holds) // 2] if holds else 0.0
        assert hold < 0.004, ("a word batch holds the store lock %.1f ms (median): every query arriving meanwhile "
                              "waits it out" % (hold * 1000))
        assert served >= 0.8 * (served + fell), "only %d of %d queries served from RAM" % (served, served + fell)
    finally:
        st.close()
