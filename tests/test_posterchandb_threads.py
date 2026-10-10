"""PosterChanDB with a writer and readers on different threads at once -- as in the relay.

The first live copy into the relay's mirror stopped 200,000 events in: "BufferError: Existing exports of data:
object cannot be re-sized". A column (bytearray / array.array) cannot grow while a numpy view of it is alive;
stats() made one WITHOUT the lock (the relay's status thread calls it every few seconds), and query() made views
that outlived its lock by the few microseconds its frame takes to end. Every test so far ran single-threaded,
which is why none could see it. Here a writer stores while readers hammer stats, query, the maintenance scan and
the mirror's count; nothing may raise, and every stored event must be there at the end.
"""
import threading
import time

from app.services.posterchandb import maintenance as M
from app.services.posterchandb import mirror as Mi
from app.services.posterchandb.store import Store
from tests.test_posterchandb_durability import _events


def test_a_writer_and_four_kinds_of_reader_never_collide(tmp_path):
    st = Store(str(tmp_path / "db"), flush_interval=3600, direct_durable=False)
    evs = _events(31, 5000)
    stop = threading.Event()
    errors = []

    def guard(fn):
        def run():
            while not stop.is_set():
                try:
                    fn()
                    time.sleep(0.001)          # a reader that never lets go would only measure starvation
                except Exception as e:      # noqa: BLE001
                    errors.append(repr(e))
                    return
        return run
    maint = M.Maintainer(st, lambda: M.Policy(retention_days=30, min_free_pct=0))
    mi = Mi.Mirror(str(tmp_path / "m"), "serve", lambda: None)
    mi.store = st
    readers = [threading.Thread(target=guard(f), daemon=True) for f in (
        st.stats,
        lambda: st.query({"kinds": [1, 7], "limit": 50}),
        lambda: maint.prune_masks(M.Policy(retention_days=30), int(time.time())),
        lambda: mi._live_count(int(time.time())),
    )]
    for t in readers:
        t.start()
    try:
        for i, ev in enumerate(evs):
            (st.copy_put if i % 2 else st.put)(dict(ev), origin="wot")
            if i % 1000 == 999:
                st.flush()
    except Exception as e:      # noqa: BLE001
        errors.append("writer: " + repr(e))
    finally:
        stop.set()
        for t in readers:
            t.join(10)
    try:
        assert not errors, errors[:3]
        assert len(st.off) == len(evs)
        assert len({len(st.seg), len(st.off), len(st.created), len(st.dead), len(st.ids) // 32}) == 1, \
            "columns grew to different lengths"
    finally:
        st.close()
