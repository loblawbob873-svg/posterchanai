#!/usr/bin/env python3
"""Does PosterChanDB serve a store many times bigger than its RAM? Built and measured, not argued.

    # 1. a synthetic store (ScaleGen: realistic kinds, tags, words, `#d~` docs) -- on a big disk, never the SSD:
    venv-unified/bin/python scripts/pcdb_scale_check.py build --path /usb/pcdb-scale/big --events 30000000
    # 2. merge its index runs, as the relay's housekeeping would have:
    venv-unified/bin/python scripts/pcdb_scale_check.py compact --path /usb/pcdb-scale/big
    # 3. open it COLD inside a memory-capped cgroup (no swap) and query it; prints latency, RSS and the cgroup peak:
    systemd-run --user --scope -q -p MemoryMax=4G -p MemorySwapMax=0 \\
        venv-unified/bin/python scripts/pcdb_scale_check.py probe --path /usb/pcdb-scale/big --queries 300

The owner's target (2026-10-16): "if the disk size of the DB is say 200GB, and I set the read/write cache to 30GB,
should be fine". The tests (tests/test_posterchandb_scales_past_ram.py) prove the mechanisms at 360k events; this is
the same question at tens of millions, at the 200:30 ratio, on a real disk.
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _size(path: str) -> int:
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs)


def build(path: str, n: int, seed: int) -> None:
    from app.services.posterchandb.store import Store
    from tests.pcdb_scale_gen import ScaleGen
    if os.path.realpath(path).startswith("/var/lib/posterchandb"):
        raise SystemExit("refusing the production directory")
    os.makedirs(path, exist_ok=True)
    g = ScaleGen(seed=seed)
    st = Store(path, flush_interval=3600, direct_durable=False)
    have = st.stats()["events"]
    t0 = time.time()
    for i in range(have, n):
        st.copy_put(g.event())
        if i % 100000 == 99999:
            st.index_pending_words()
            if i % 1000000 == 999999:
                st.flush()          # what the relay's flusher does every few seconds; unflushed, the build is a RAM test
                print("  rss %.0f MB" % (_status("RssAnon") / 2 ** 20), flush=True)
            el = time.time() - t0
            print("%d events, %.0f/s, %.1f GB on disk" % (i + 1, (i + 1 - have) / el, _size(path) / 2 ** 30), flush=True)
    st.index_pending_words()
    st.close()
    print("built %d events, %.1f GB, %.0f s" % (n, _size(path) / 2 ** 30, time.time() - t0), flush=True)


def compact(path: str) -> None:
    """Merge the index runs the way the relay's housekeeping does in the background (a build made with flushes only
    leaves dozens of runs per index -- not the shape any running node is in, and every lookup pays one search each)."""
    from app.services.posterchandb.store import Store
    st = Store(path, flush_interval=3600, direct_durable=False)
    t0, n = time.time(), 0
    while True:
        k = st.merge_disk_runs(max_steps=64)
        n += k
        print("  %d merges, %.0f s" % (n, time.time() - t0), flush=True)
        if not k:
            break
    from app.services.posterchandb import disk
    wrote = sum(disk.save_fences(st.path, r, len(lv.dtypes)) for lv in st._leveled() for r in lv.runs
                if r.name is not None)
    print("  fence files written for %d older runs" % wrote, flush=True)
    st.close()
    print("compacted: %d merges in %.0f s" % (n, time.time() - t0), flush=True)


def _status(key: str) -> int:
    for line in open("/proc/self/status"):
        if line.startswith(key):
            return int(line.split()[1]) * 1024
    return 0


def _cgroup_peak() -> int:
    try:
        cg = open("/proc/self/cgroup").read().split("::")[-1].strip()
        return int(open("/sys/fs/cgroup%s/memory.peak" % cg).read())
    except Exception:
        return 0


def probe(path: str, queries: int, seed: int) -> None:
    from app.services.posterchandb.store import Store
    t0 = time.time()
    st = Store(path, flush_interval=3600, direct_durable=False, admit=lambda nbytes: False)   # cold: the worst case
    opened = time.time() - t0
    s = st.stats()
    n = s["events"]
    r = random.Random(seed)
    lat: dict = {}

    def q(what, flt):
        a = time.perf_counter()
        st.query(flt)
        lat.setdefault(what, []).append((time.perf_counter() - a) * 1000)
    for _ in range(queries):
        au = st.pubkey_of(r.randrange(min(n, 20000)))
        q("timeline", {"kinds": [1, 6], "limit": 120})
        q("author", {"authors": [au], "limit": 50})
        q("tag", {"#t": ["w%d" % r.randrange(500)], "limit": 50})
        q("ids", {"ids": [st._id_hex(r.randrange(n)) for _ in range(5)]})
        q("doc-prefix", {"#d~": ["pcai:t:notes:"], "limit": 100})
        if r.random() < 0.2:
            q("search", {"search": "w%d w%d" % (r.randrange(2000), r.randrange(2000)), "limit": 20})
    print("store: %d events, %.1f GB on disk, opened cold in %.1f s (replayed %s)" % (
        n, _size(path) / 2 ** 30, opened, st.last_open.get("replayed")))
    for what, xs in sorted(lat.items()):
        xs.sort()
        print("  %-11s n=%-4d p50 %7.1f ms  p99 %8.1f ms  max %8.1f ms" % (
            what, len(xs), statistics.median(xs), xs[max(0, int(len(xs) * 0.99) - 1)], xs[-1]))
    rb = next((int(l.split()[1]) for l in open("/proc/self/io") if l.startswith("read_bytes")), 0)
    print("RssAnon %.0f MB, VmRSS %.0f MB, cgroup peak %.0f MB, read from disk %.1f GB" % (
        _status("RssAnon") / 2 ** 20, _status("VmRSS") / 2 ** 20, _cgroup_peak() / 2 ** 20, rb / 2 ** 30))
    st.close(take_snapshot=False)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    b = sub.add_parser("build"); b.add_argument("--path", required=True); b.add_argument("--events", type=int, required=True)
    p = sub.add_parser("probe"); p.add_argument("--path", required=True); p.add_argument("--queries", type=int, default=300)
    c = sub.add_parser("compact"); c.add_argument("--path", required=True)
    for x in (b, p):
        x.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    if a.mode == "build":
        build(a.path, a.events, a.seed)
    elif a.mode == "compact":
        compact(a.path)
    else:
        probe(a.path, a.queries, a.seed)


if __name__ == "__main__":
    main()
