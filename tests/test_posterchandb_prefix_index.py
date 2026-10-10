"""The `#d~` prefix index answers exactly, and a table load's repeated pages do not redo the work.

Every app table loads as `#d~ pcai:t:<table>:` pages of 1000. The index used to walk every match in Python and sort
them again for EACH page, under the store lock: a 388k-row table took 20 minutes to load and the relay sat at 100%
CPU meanwhile (2026-10-10). It now finds the range with two bisects over a per-run view and caches the sorted seq set
per prefix until a run changes.
"""
import random

import numpy as np

from app.services.posterchandb.store import _Prefix


def _brute(pairs, pre):
    return sorted({q for v, q in pairs if v.startswith(pre)})


def test_prefix_matches_brute_force_across_runs_delta_and_unicode():
    r = random.Random(3)
    alphabet = ["a", "b", "z", "é", "\U0010FFFF", ":", "t"]
    pairs = []
    p = _Prefix()
    seq = 0
    for round_ in range(6):
        for _ in range(r.randint(50, 400)):
            v = "".join(r.choice(alphabet) for _ in range(r.randint(0, 6)))
            pairs.append((v, seq))
            p.add(v, seq)
            seq += 1
        if round_ < 5:
            p.merge()                      # several runs, then an unmerged delta on top
    for pre in ["", "a", "ab", "z", "é", "\U0010FFFF", "a\U0010FFFF", "t:", "zzzzzzz", "b:é"]:
        got = p.seqs(pre)
        assert got.dtype == np.uint32
        assert list(got) == _brute(pairs, pre), repr(pre)
        assert p.prefix(pre) == _brute(pairs, pre)


def test_a_dead_event_merged_away_disappears_and_the_cache_follows_the_runs():
    p = _Prefix()
    for i in range(10):
        p.add("pcai:t:x:%d" % i, i)
    p.merge()
    assert list(p.seqs("pcai:t:x:")) == list(range(10))
    dead = bytearray(13)
    dead[3] = 1
    for i in range(10, 13):
        p.add("pcai:t:x:%d" % i, i)
    p.merge(dead)                          # a 3-row run beside a 10-row one merges (3*4 >= 10), dropping seq 3
    assert len(p.runs) == 1
    got = list(p.seqs("pcai:t:x:"))        # the runs changed: the cached answer must not be reused
    assert 3 not in got and got == [i for i in range(13) if i != 3]


def test_repeated_pages_reuse_one_sorted_set():
    p = _Prefix()
    for i in range(50000):
        p.add("pcai:t:big:%06d" % (50000 - i), i)      # value order is the reverse of seq order
    p.merge()
    first = p.seqs("pcai:t:big:")
    assert len(first) == 50000 and bool(np.all(first[1:] > first[:-1]))
    for _ in range(5):
        assert p.seqs("pcai:t:big:") is first, "a page rebuilt the prefix set instead of reusing it"
    p.add("pcai:t:big:x", 50000)                        # a write lands mid-load: delta only, runs untouched
    got = p.seqs("pcai:t:big:")
    assert len(got) == 50001 and got[-1] == 50000
    assert p.seqs("pcai:t:big:") is not first or len(first) == 50000
