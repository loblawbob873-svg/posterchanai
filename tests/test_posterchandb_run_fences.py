"""Fence pointers: a lookup in a run on disk reads ONE page of keys, not a binary search's worth.

2026-10-18, a 30M-event store probed cold at the 200:30 RAM ratio on spinning disks: an id lookup cost ~80 ms --
a binary search over a mapped 240 MB key array is ~20 random page faults, every one a seek. The first key of each
4 KB page is kept in RAM (written beside the run when it is written), so a search is done in RAM down to one page.
"""
import os
import random

import numpy as np

from app.services.posterchandb import disk


def _run(tmp_path, keys, name="id-1"):
    keys = np.sort(np.asarray(keys, dtype=np.uint64))
    return disk.write_run([keys, np.arange(len(keys), dtype=np.uint32)], disk.KV_DTYPES, str(tmp_path), name), keys


def test_the_fenced_search_is_searchsorted(tmp_path):
    r = random.Random(3)
    # duplicates that straddle fence boundaries, runs of one key longer than a fence, gaps, both ends
    keys = [r.randrange(50_000) for _ in range(30_000)] + [7] * 1500 + [2 ** 63] * 3 + [0] * 600
    run, k = _run(tmp_path, keys)
    assert run.fences is not None and len(run.fences) == -(-len(k) // disk.FENCE)
    probes = list(k[:: 97]) + [0, 1, 6, 7, 8, 49_999, 50_000, 2 ** 63, 2 ** 64 - 1] + \
        [r.randrange(60_000) for _ in range(2000)] + [int(k[i]) for i in range(0, len(k), disk.FENCE)]
    for p in probes:
        p = np.uint64(p)
        for side in ("left", "right"):
            assert disk.search(run, p, side) == int(np.searchsorted(k, p, side)), (int(p), side)
        lo, hi = int(np.searchsorted(k, p, "left")), int(np.searchsorted(k, p, "right"))
        assert disk.kv_get(run, int(p)) == [int(x) for x in run[1][lo:hi]]


def test_fences_are_written_with_the_run_and_read_back(tmp_path):
    run, k = _run(tmp_path, range(0, 20_000, 3))
    fp = os.path.join(str(tmp_path), "run-id-1.2.bin")
    assert os.path.exists(fp)
    again = disk.map_run(str(tmp_path), "id-1", disk.KV_DTYPES)
    assert again.fences is not None and np.array_equal(again.fences, k[::disk.FENCE])
    with open(fp, "r+b") as f:                       # a torn fence file is ignored, never trusted
        f.truncate(16)
    torn = disk.map_run(str(tmp_path), "id-1", disk.KV_DTYPES)
    assert torn.fences is None
    assert disk.search(torn, np.uint64(300), "left") == int(np.searchsorted(k, np.uint64(300)))


class _Spy(np.ndarray):
    """The run's key array, recording how many keys each np.searchsorted on it had to search. (Page residency cannot
    be measured where tests write: /tmp is tmpfs, whose pages never leave the cache, and the kernel's fault-around maps
    16 cached pages per fault whether or not they were needed.)"""
    spans: list = []

    def __array_function__(self, func, types, args, kwargs):
        if func is np.searchsorted:
            _Spy.spans.append(len(args[0]))
        args = tuple(a.view(np.ndarray) if isinstance(a, _Spy) else a for a in args)
        return func(*args, **kwargs)


def test_a_lookup_searches_one_page_of_keys(tmp_path):
    n = 2_000_000                                    # 16 MB of keys: a binary search over all of them is ~12 pages
    run, k = _run(tmp_path, np.arange(0, 3 * n, 3))
    spy = disk.Run([run[0].view(_Spy), run[1]], run.name)
    spy.fences = run.fences
    r = random.Random(1)
    _Spy.spans = []
    for _ in range(200):
        key = r.randrange(3 * n)
        assert disk.kv_get(spy, key) == ([key // 3] if key % 3 == 0 else [])
    assert _Spy.spans and max(_Spy.spans) <= disk.FENCE + 1, \
        "a lookup searched %d keys on disk (one page is %d)" % (max(_Spy.spans), disk.FENCE)
