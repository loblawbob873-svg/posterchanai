"""A relay database many times the size of the machine's RAM: the index lives in FILES, and a snapshot writes what CHANGED.

Asked 2026-10-16: "we need to make sure our relay database can grow to hundreds of GB's without any issues with writing
to disk ... if the disk size of the DB is say 200GB, and if I set the read/write cache to 30GB, should be fine". It was
not. Measured on poster.place (4.72M events): every per-event column, the id lookup, the tag and word postings and the
`#d~` prefix index (1.36M Python tuples) were held in RAM -- ~200 bytes an event on top of the segment cache -- and the
snapshot rewrote ALL of it (1.24 GB) each time. At 200 GB (~260M events) that is ~50 GB of RAM before a single byte of
cache, and a 50 GB write per snapshot. The cache setting governed only the event bytes; nothing bounded the rest.

Now the index is file-backed (columns mapped copy-on-write, sealed runs mapped read-only): the kernel keeps what is read
and drops what is not, exactly like the segment files, and a snapshot appends the new tail and a small journal of
changed pages. These tests hold both properties against the SHIPPED store at two sizes, measured -- anonymous memory of
a process that opened the store and queried it, and the bytes a snapshot actually wrote.
"""
import copy
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.services.posterchandb.store import Store  # noqa: E402
from tests.pcdb_scale_gen import ScaleGen  # noqa: E402

SEG = 8 * 1024 * 1024      # small segments: the event BYTES are the cache's business, not what is measured here


def _build(path, n, seed=1):
    g = ScaleGen(seed=seed)
    st = Store(path, flush_interval=3600, direct_durable=False, segment_bytes=SEG)
    for i in range(n):
        st.copy_put(g.event())
        if i % 50000 == 49999:
            st.index_pending_words()
            st.flush()
    st.index_pending_words()
    st.close()
    return g


# BUILT ONCE PER MODULE. Building is ~90 us an event, and this file used to build 1.13M of them (the 360k store twice):
# ~105 s of its ~115 s. Two stores now, each built the same way as before; a test that WRITES works on its own copy
# (a file copy of the store a clean close left), so no test sees another's writes.
SMALL_N, BIG_N = 60_000, 360_000


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    made = {}

    def get(name):
        if name not in made:
            path = str(tmp_path_factory.mktemp("pcdb-" + name) / "s")
            made[name] = (path, _build(path, {"small": SMALL_N, "big": BIG_N}[name]))
        return made[name]
    return get


def _copy(built, name, into):
    """A private copy of a built store, and its generator exactly where the build left it (each copy continues the same
    stream, as a test that built its own store did)."""
    src, _ = built(name)
    dst = os.path.join(into, "s")
    shutil.copytree(src, dst)
    return dst, copy.deepcopy(built(name)[1])


PROBE = r"""
import sys, os
sys.path.insert(0, %(root)r)
from app.services.posterchandb.store import Store
st = Store(%(path)r, flush_interval=3600, direct_durable=False, segment_bytes=%(seg)d, admit=lambda n: False)
assert st.last_open["replayed"] == 0, st.last_open
au = st.pubkey_of(0); n = st.stats()["events"]
for flt in ({"authors": [au], "limit": 50}, {"kinds": [1], "limit": 200}, {"#t": ["w1"]}, {"search": "w2 w3"},
            {"#d~": ["pcai:t:notes:"], "limit": 100}, {"ids": [st._id_hex(n // 2), st._id_hex(n - 1)]}):
    st.query(flt)
st.match({"kinds": [7]})
for l in open("/proc/self/status"):
    if l.startswith("RssAnon"):
        print(int(l.split()[1]) * 1024)
"""


def _anon(path):
    r = subprocess.run([sys.executable, "-c", PROBE % {"root": ROOT, "path": path, "seg": SEG}],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-3000:]
    return int(r.stdout.split()[-1])


def test_a_bigger_store_does_not_take_more_ram_to_open_and_query(built):
    small_n, big_n = SMALL_N, BIG_N
    a_small, a_big = _anon(built("small")[0]), _anon(built("big")[0])
    per_event = (a_big - a_small) / (big_n - small_n)
    # Before: ~200 B an event (columns 82, postings 65, the `#d~` tuples ~60) -- 60 MB more for these 300k events.
    assert per_event < 16, ("anonymous RAM grows with the store: %.0f bytes/event (%d -> %d bytes)"
                            % (per_event, a_small, a_big))


def _written():
    for l in open("/proc/self/io"):
        if l.startswith("wchar"):
            return int(l.split()[1])


def test_a_snapshot_writes_what_changed_not_the_whole_index(built):
    with tempfile.TemporaryDirectory(prefix="pcdb-ckpt-") as d:
        path, g = _copy(built, "big", d)
        st = Store(path, flush_interval=3600, direct_durable=False, segment_bytes=SEG)
        try:
            for _ in range(2000):
                st.copy_put(g.event())
            st.index_pending_words()
            w0 = _written()
            assert st.snapshot() is not None
            wrote = _written() - w0
        finally:
            st.close()
    # 2000 events are ~1 MB of log and index. Before: the whole index again (~70 MB at these 360k events, 1.24 GB live).
    assert wrote < 4 * 1024 * 1024, "a snapshot after 2000 new events wrote %.1f MB" % (wrote / 2 ** 20)


def test_a_clean_stop_and_start_replays_nothing(built):
    """A deploy restarts the relay: with a snapshot at every clean stop (cheap now), the start replays no log at all."""
    with tempfile.TemporaryDirectory(prefix="pcdb-stop-") as d:
        path, g = _copy(built, "small", d)
        st = Store(path, flush_interval=3600, direct_durable=False, segment_bytes=SEG)
        for _ in range(500):
            st.copy_put(g.event())
        st.close(take_snapshot=False)          # what the relay passes (systemd gives a stop 10 s)
        st = Store(path, flush_interval=3600, direct_durable=False, segment_bytes=SEG)
        try:
            assert st.last_open["snapshot"] and st.last_open["replayed"] == 0, st.last_open
            assert st.stats()["events"] == SMALL_N + 500
        finally:
            st.close()


@pytest.mark.skipif(not shutil.which("systemd-run") or subprocess.run(
    ["systemd-run", "--user", "--scope", "-q", "true"], capture_output=True).returncode != 0,
    reason="needs a user systemd to put the probe in a memory-limited cgroup")
def test_a_cheap_vps_opens_and_queries_a_store_bigger_than_its_ram(built):
    """'we also need to make sure people can run posterchan on a cheap VPS' (2026-10-16). The probe runs in a cgroup
    capped BELOW the size of the store's files (no swap): the index is read through the page cache, which the kernel
    reclaims inside the cap, so only what is genuinely anonymous has to fit. Before: ~186 bytes an event of anonymous
    index, and this cap was exceeded (the kernel kills the process)."""
    path = built("big")[0]           # opened read-only by the probe: the same 360k store the RAM test measured
    size = sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path))
    cap = 88 * 1024 * 1024
    assert size > 1.4 * cap, "the store must be bigger than the cap for this to mean anything (%d)" % size
    r = subprocess.run(["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=%d" % cap, "-p", "MemorySwapMax=0",
                        sys.executable, "-c", PROBE % {"root": ROOT, "path": path, "seg": SEG}],
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, ("the store could not be opened and queried within %d MB: rc=%s %s"
                               % (cap >> 20, r.returncode, r.stderr[-2000:]))


def test_the_admin_read_cache_setting_bounds_the_index_in_ram_too(built):
    """'ram usage should be tuneable in admin settings' -- Admin -> Relay -> Database -> Read cache (MB) is ONE number
    for the event cache AND the index pages a query pulled in. Over it, the governor hands the index's pages back."""
    from app.services.posterchandb import cache, tuning
    with tempfile.TemporaryDirectory(prefix="pcdb-trim-") as d:
        path, _ = _copy(built, "big", d)
        gov = cache.CacheGovernor(budget_mb=0, interval=3600)
        st = Store(path, flush_interval=3600, direct_durable=False, segment_bytes=SEG, admit=gov.admit)
        gov.store = st
        try:
            n = len(st.off)
            for i in range(0, n, 997):                     # touch the index everywhere
                st.query({"ids": [st._id_hex(i)]})
                st.query({"authors": [st.pubkey_of(i)], "limit": 5})
            st.query({"search": "w1 w2"})
            st.match({"kinds": [1]})
            before = gov.tick()["index_mb"]
            assert before >= 4, "the queries did not pull the index into RAM (%s MB)" % before
            tuning.apply(st, gov, None, {"posterchandb_read_cache_mb": 1})      # what Save in the admin panel does
            res = gov.tick()
            assert res.get("trimmed_mb", 0) > 0 and res["index_mb"] < before / 2, (before, res)
            assert len(st.query({"kinds": [1], "limit": 50})) == 50                # and it still answers
        finally:
            st.close()
