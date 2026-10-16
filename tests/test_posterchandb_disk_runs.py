"""The on-disk pieces of PosterChanDB's index (disk.py) give EXACTLY the answers of the in-RAM structures they replace.

The streaming merges are run with pieces a few entries long, so every boundary case -- a key whose postings span many
pieces, equal keys at a piece edge, equal `d` values across runs, a run exhausted first -- happens hundreds of times.
"""
import os
import random
import tempfile

import numpy as np
import pytest

from app.services.posterchandb import disk


@pytest.fixture
def tiny(monkeypatch):
    monkeypatch.setattr(disk, "CHUNK", 7)
    monkeypatch.setattr(disk, "CHUNK_POSTINGS", 11)
    monkeypatch.setattr(disk, "STR_CHUNK_BYTES", 64)


def _postings(r, keys, lo, hi, hot=None):
    d = {}
    for s in range(lo, hi):
        for _ in range(r.randint(1, 3)):
            k = hot if (hot is not None and r.random() < .3) else r.choice(keys)
            if s not in d.setdefault(k, []):
                d[k].append(s)
    ks = sorted(d)
    starts = np.zeros(len(ks) + 1, np.int64)
    np.cumsum([len(d[k]) for k in ks], out=starts[1:])
    return (np.array(ks, np.uint64), starts, np.array([s for k in ks for s in d[k]], np.uint32))


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("on_disk", [False, True])
def test_streamed_posting_merge_equals_the_in_ram_merge(tiny, seed, on_disk):
    r = random.Random(seed)
    keys = [r.getrandbits(63) for _ in range(r.randint(1, 40))]
    hot = keys[0]
    a = _postings(r, keys, 0, r.randint(0, 120), hot)
    b = _postings(r, keys + [r.getrandbits(63) for _ in range(5)], 200, 200 + r.randint(0, 120), hot)
    dead = np.zeros(400, np.uint8)
    dead[r.sample(range(400), 60)] = 1
    live = lambda p: dead[p] == 0
    want = disk.merge_postings_mem(*a, *b, live)
    with tempfile.TemporaryDirectory() as d:
        sink = disk.RunSink(disk.POSTING_DTYPES, d if on_disk else None, "t-1")
        got = disk.merge_postings(a, b, sink, live)
        for w, g in zip(want, got):
            assert np.array_equal(np.asarray(w), np.asarray(g)), (seed, w, g)


@pytest.mark.parametrize("seed", range(25))
def test_streamed_kv_merge_keeps_every_entry_in_order(tiny, seed):
    r = random.Random(seed)
    pool = [r.getrandbits(63) for _ in range(12)]
    pa = [(r.choice(pool), i) for i in range(r.randint(0, 60))]
    pb = [(r.choice(pool), 100 + i) for i in range(r.randint(0, 60))]
    a, b = disk.kv_from_pairs(pa), disk.kv_from_pairs(pb)
    keep = lambda v: (v % 5) != 0
    got = disk.merge_kv(a, b, disk.RunSink(disk.KV_DTYPES), keep)
    want = sorted([(k, v) for k, v in pa + pb if v % 5], key=lambda x: x[0])   # stable: A before B, arrival order
    assert list(zip(got[0].tolist(), got[1].tolist())) == want


@pytest.mark.parametrize("seed", range(40))
def test_streamed_string_merge_is_exact_byte_order_then_seq(tiny, seed):
    r = random.Random(seed)
    vals = ["", "a", "a\x00", "ab", "pcai:t:users:", "pcai:t:users:x", "é", "\ud800", "z" * r.randint(1, 90), "pcai:note:1"]
    pa = [(r.choice(vals), i) for i in range(r.randint(0, 50))]
    pb = [(r.choice(vals), 100 + i) for i in range(r.randint(0, 50))]
    a, b = disk.str_from_pairs(pa), disk.str_from_pairs(pb)
    live = lambda s: (s % 7) != 3
    with tempfile.TemporaryDirectory() as d:
        got = disk.merge_strings(a, b, disk.RunSink(disk.STR_DTYPES, d, "s-1"), live)
        out = [(disk.dec(disk.str_at(got, i)), int(got[2][i])) for i in range(len(got[2]))]
    want = sorted(((disk.enc(v), q) for v, q in pa + pb if q % 7 != 3))
    assert out == [(disk.dec(v), q) for v, q in want]
    for pre in ("", "a", "pcai:t:", "pcai:t:users:", "z", "é"):
        i, j = disk.StrRuns.range(got if len(got[2]) else a, disk.enc(pre), None)


def test_a_column_survives_growth_checkpoint_and_a_crash_before_the_next_one():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "c.bin")
        c = disk.Column(p, "Q")
        for i in range(100_000):
            c.append(i * 3)
        assert len(c) == 100_000 and c[99_999] == 299_997 and int(c.view()[12345]) == 37035
        c.write_tail(); c.sync(); c.committed()
        c[5] = 7                                    # a change below the checkpoint: private until the next one
        c.append(1)
        c.close()
        c2 = disk.Column(p, "Q", length=100_000)     # "crash": reopen at the checkpoint
        assert c2[5] == 15 and len(c2) == 100_000
        c2[5] = 7
        entries = c2.journal_entries()
        assert len(entries) == 1 and entries[0][0] == 0
        for off, data in entries:
            disk.pwrite_all(c2.fd, data, off)
        c2.sync(); c2.committed(); c2.close()
        assert disk.Column(p, "Q", length=100_000)[5] == 7
