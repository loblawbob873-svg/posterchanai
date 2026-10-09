"""PosterChanDB's RAM cache: "if machine RAM is running low, drain the cache, and be smart about filling it".

A store with many small segments and a FAKE /proc/meminfo + PSI the test squeezes and relaxes. Every rule is
checked on what a reader gets (identical query answers, warm or cold, before and after a restart) and on what
is resident afterwards.
"""
import os
import time


from app.services.posterchandb.cache import MB, CacheGovernor, meminfo, psi_some_avg10
from app.services.posterchandb.store import Store
from tests.test_posterchandb import hx, mk

NOW = int(time.time()) - 120          # real time: the relay refuses events from the future
GB = 1024 * MB


class Box:
    """A machine whose free memory the test controls."""

    def __init__(self, total=8 * GB, available=6 * GB, psi=0.0):
        self.total, self.available, self.p = total, available, psi
        self.t = 1000.0

    def mem(self):
        return {"total": self.total, "available": self.available}

    def psi(self):
        return self.p

    def clock(self):
        return self.t


def build(tmp_path, n=240, admit=None):
    s = Store(str(tmp_path / "db"), flush_interval=3600, direct_durable=False, segment_bytes=16384, admit=admit)
    evs = []
    for i in range(n):
        e = mk(kind=1, created_at=NOW - i, content=hx(400), tags=[["t", "tag%d" % (i % 7)]])
        s.put(e, origin="wot")
        evs.append(e)
        if i % 10 == 9:
            s.flush()
    s.flush()
    return s, evs


def answer(s):
    return [e["id"] for e in s.query({"limit": 5000}, now=NOW)], [e["id"] for e in s.query({"#t": ["tag3"]}, now=NOW)]


def gov(s, box, **kw):
    return CacheGovernor(s, mem=box.mem, psi=box.psi, clock=box.clock, **kw)


def closed_resident(s):
    return [sid for sid, a in s.arenas.items() if a is not None and sid != s._active]


def test_a_cold_segment_answers_exactly_what_a_warm_one_did(tmp_path):
    s, _ = build(tmp_path)
    warm = answer(s)
    assert len(closed_resident(s)) > 5
    for sid in list(s.arenas):
        s.evict(sid)
    assert closed_resident(s) == [] and s.arenas[s._active] is not None, "the active segment must stay resident"
    assert answer(s) == warm
    s.close()
    s = Store(str(tmp_path / "db"), admit=lambda n: False)
    assert answer(s) == warm
    s.close()


def test_low_memory_drains_the_least_recently_read_segments_first(tmp_path):
    s, evs = build(tmp_path)
    sids = sorted(closed_resident(s))
    hot = sids[0]
    for e in evs:                                     # read one OLD segment's events: it becomes the most recent
        q = s.seq_of(e["id"])
        if s.seg[q] == hot:
            s.get(q)
    box = Box(total=8 * GB, available=600 * MB)       # below the 819 MB low-water mark
    r = gov(s, box).tick()
    assert r["pressure"] and r["drained"]
    assert hot not in r["drained"] or len(r["drained"]) == len(sids), "the segment being read went first"
    assert s._active not in r["drained"]
    s.close()


def test_pressure_from_the_kernel_counts_even_with_memory_free(tmp_path):
    s, _ = build(tmp_path)
    box = Box(available=6 * GB, psi=35.0)            # stalls on memory: the box is thrashing somewhere
    assert gov(s, box).tick()["drained"]
    s.close()


def test_the_configured_budget_is_a_ceiling(tmp_path):
    s, _ = build(tmp_path)
    before = s.resident_bytes()
    g = gov(s, Box(), budget_mb=before / MB / 2)
    g.tick()
    assert s.resident_bytes() <= before / 2 + 16384
    s.close()


def test_plenty_of_memory_drains_nothing(tmp_path):
    s, _ = build(tmp_path)
    r = gov(s, Box()).tick()
    assert r["drained"] == [] and not r["pressure"]
    s.close()


def test_filling_waits_for_real_demand_room_and_the_cooldown(tmp_path):
    s, evs = build(tmp_path)
    box = Box(available=600 * MB)
    g = gov(s, box, fill_hits=200, cooldown=300)   # reads are counted per record (~25 in a segment here)
    drained = g.tick()["drained"]
    target = drained[0]
    box.available = 6 * GB                            # memory comes back
    seqs = [s.seq_of(e["id"]) for e in evs if s.seg[s.seq_of(e["id"])] == target]

    def read_it(times):
        for _ in range(times):
            for q in seqs:
                s.get(q)
    read_it(1)
    box.t += 400
    assert g.tick()["filled"] == [], "a segment read a little stays cold"
    read_it(20)
    box.t = 1000 + 10                                 # inside the cooldown after its drain
    assert target not in g.tick()["filled"], "refilled what was just drained (thrash)"
    read_it(20)
    box.t = 1000 + 400
    assert target in g.tick()["filled"]
    assert s.arenas[target] is not None
    s.close()


def test_filling_never_pushes_the_machine_under_its_low_water_mark(tmp_path):
    s, evs = build(tmp_path)
    for sid in list(s.arenas):
        s.evict(sid)
    for e in evs:
        s.get(s.seq_of(e["id"]))
        s.get(s.seq_of(e["id"]))
    box = Box(total=8 * GB, available=int(1.6 * GB))    # exactly 2x low water (819 MB): any load goes under it
    box.t = 10_000
    g = gov(s, box, fill_hits=1)
    assert g.tick()["filled"] == []
    s.close()


def test_a_store_opening_on_a_short_box_starts_cold_and_still_answers(tmp_path):
    s, _ = build(tmp_path)
    warm = answer(s)
    s.close()
    box = Box(available=900 * MB)
    g = CacheGovernor(None, mem=box.mem, psi=box.psi, clock=box.clock)
    s = Store(str(tmp_path / "db"), admit=g.admit)
    g.store = s
    assert closed_resident(s) == []
    assert answer(s) == warm
    s.close()


def test_a_cold_segment_can_be_compacted_and_the_result_survives_a_restart(tmp_path):
    s, evs = build(tmp_path)
    victims = [e for e in evs if s.seg[s.seq_of(e["id"])] == 1][1:]
    s.kill([s.seq_of(e["id"]) for e in victims])
    s.flush()
    s.evict(1)
    assert s.compact(sid=1, pace=lambda *a: None)["compacted"] == 1
    want = {e["id"] for e in evs} - {e["id"] for e in victims}
    assert set(answer(s)[0]) == want
    s.close()
    s = Store(str(tmp_path / "db"))
    assert set(answer(s)[0]) == want
    s.close()


def test_the_machine_readings_parse_the_real_kernel_formats(tmp_path):
    mi = tmp_path / "meminfo"
    mi.write_text("MemTotal:       65536000 kB\nMemFree:  100 kB\nMemAvailable:   1024000 kB\n")
    assert meminfo(str(mi)) == {"total": 65536000 * 1024, "available": 1024000 * 1024}
    ps = tmp_path / "psi"
    ps.write_text("some avg10=12.50 avg60=3.00 avg300=1.00 total=123\nfull avg10=1.00 avg60=0.00 avg300=0.00 total=9\n")
    assert psi_some_avg10(str(ps)) == 12.5
    assert psi_some_avg10(str(tmp_path / "missing")) is None
    if os.path.exists("/proc/meminfo"):
        m = meminfo()
        assert m["total"] > 0 and 0 < m["available"] <= m["total"]


def test_the_cache_thread_never_raises_into_the_relay(tmp_path):
    s, _ = build(tmp_path, n=20)
    logs = []

    def broken():
        raise RuntimeError("no /proc")
    g = CacheGovernor(s, mem=broken, interval=0.01, log=logs.append)
    g.start()
    import time
    time.sleep(0.1)
    g.stop()
    assert any("cache tick failed" in l for l in logs) and not g._thread.is_alive()
    s.close()
