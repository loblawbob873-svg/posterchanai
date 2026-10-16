"""A checkpoint interrupted at ANY step leaves a store that opens and answers exactly like a full replay of its log.

checkpoint.py's order is the whole guarantee: log fsynced -> run files and new column rows written where the last
checkpoint does not look -> changed pages journaled -> manifest replaced -> journal applied -> mappings rebuilt. Each
step here is made to fail (the process "dies": no close, no flush), with a history that exercises every kind of
change below the last checkpoint -- kills (NIP-09, superseded versions, auto-clean) and a compaction's relocations.
"""
import random
import time

import pytest

from app.services.posterchandb import checkpoint, disk
from app.services.posterchandb.store import Store
from tests.pcdb_gen import Gen

def _copy_store(p):
    """A copy of a store directory, for a second Store to replay beside the first (a directory takes one Store)."""
    import shutil
    dst = str(p).rstrip("/") + "-full"
    shutil.copytree(p, dst, ignore=shutil.ignore_patterns("STORE.lock"))
    return dst



def _answers(store, gen, n=200, seed=11):
    gen.r = random.Random(seed)
    now = int(time.time())
    return [[e["id"] for e in store.query(gen.flt(), now=now)] for _ in range(n)]


class Boom(Exception):
    pass


def _fail_nth(monkeypatch, mod, name, n):
    real = getattr(mod, name)
    calls = {"n": 0}

    def f(*a, **k):
        calls["n"] += 1
        if calls["n"] == n:
            raise Boom(name)
        return real(*a, **k)
    monkeypatch.setattr(mod, name, f)


POINTS = [("journal", lambda mp: _fail_nth(mp, checkpoint, "_write_journal", 1)),
          ("manifest", lambda mp: _fail_nth(mp, checkpoint, "_atomic_json", 1)),
          ("apply", lambda mp: _fail_nth(mp, disk, "pwrite_all", 2)),
          ("remap", lambda mp: _fail_nth(mp, disk.Column, "committed", 3)),
          ("tail", lambda mp: _fail_nth(mp, disk.Column, "write_tail", 4))]


@pytest.mark.parametrize("point", [p for p, _ in POINTS])
def test_a_checkpoint_cut_short_anywhere_opens_like_a_full_replay(tmp_path, monkeypatch, point):
    p = str(tmp_path / "db")
    s = Store(p, flush_interval=3600, direct_durable=False, segment_bytes=40000)
    g = Gen(3)
    for i in range(1200):
        s.put(g.event(), origin=g.r.choice(["direct", "wot"]))
    s.snapshot()                                       # checkpoint 1
    for i in range(600):                                # more history: new rows, kills of OLD rows, relocations
        s.put(g.event(), origin="wot")
    s.kill([q for q in range(0, 400, 3) if not s.dead[q]])
    s.flush()
    sid = s.compaction_candidate() or min(x for x in s.seg_size if x != s._active)
    dict(POINTS)[point](monkeypatch)
    try:
        s.compact(sid=sid, pace=lambda *a: None)       # takes its own checkpoint (the one that is cut short)
        s.snapshot()
    except Boom:
        pass
    monkeypatch.undo()
    s._file.close()                                     # the process dies
    s._release_dirlock()                 # ...and the kernel drops its lock
    full_dir = _copy_store(p)                    # one Store per directory: the full replay gets a copy
    fast = Store(p)
    full = Store(full_dir, snapshots=False)
    try:
        assert fast.last_open["snapshot"], (point, fast.last_open)
        assert _answers(fast, g) == _answers(full, g), point
        # and it keeps working: more writes, another checkpoint, another reopen
        for _ in range(100):
            ev = g.event()
            assert fast.put(dict(ev), origin="wot") == full.put(dict(ev), origin="wot")
        fast.snapshot()
        fast.close()
        again = Store(p)
        assert again.last_open["replayed"] == 0 and _answers(again, g) == _answers(full, g), point
        again.close()
    finally:
        full.close()
