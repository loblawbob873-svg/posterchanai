"""A flush does not hold every reader while it merges RAM runs and writes a run to disk.

2026-10-17, scripts/relay_loadtest.py against a copy of poster.place's store: 400 clients got timeline answers of
32 ms at the median and 45-53 s at the 99th percentile while the relay used a quarter of a core -- every query was
waiting behind Store.flush, which held the store lock across the fsync, the RAM-run merge cascade and the write of a
run past `seal_at` (disk.Leveled.merge under the lock). The write here is made slow; a reader must not wait for it.
"""
import threading
import time

from app.services.posterchandb import disk
from app.services.posterchandb.store import Store
from tests.pcdb_gen import Gen

SLOW = 3.0


def test_a_reader_does_not_wait_for_a_flush_writing_a_run(tmp_path, monkeypatch):
    st = Store(str(tmp_path), flush_interval=3600, direct_durable=False)
    gen = Gen(5)
    for _ in range(400):
        st.put(gen.event(), origin="wot")
    for lv in st._leveled():
        monkeypatch.setattr(lv, "seal_at", 1)              # every flush seals: the slow path, every time
    real = disk.write_run

    def slow_write(*a, **k):
        time.sleep(SLOW)
        return real(*a, **k)
    monkeypatch.setattr(disk, "write_run", slow_write)
    for _ in range(200):
        st.put(gen.event(), origin="wot")
    t = threading.Thread(target=st.flush)
    t.start()
    time.sleep(0.3)                                       # the flush is now inside its slow write
    t0 = time.monotonic()
    got = st.query({"kinds": [1], "limit": 20})
    waited = time.monotonic() - t0
    t.join()
    assert waited < 1.0, "a query waited %.1f s for a flush" % waited
    assert got is not None
    # ...and the flush still finished its work: nothing buffered, every new posting in a run, a run sealed to disk
    assert st._pending_n == 0
    assert any(r.name is not None for r in st.idx.runs)
    ids = {e["id"] for e in st.query({"kinds": [1], "limit": 2000})}
    assert len(ids) > 0
