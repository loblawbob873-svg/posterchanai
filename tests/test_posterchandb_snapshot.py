"""Index snapshots: a restart loads the finished indexes instead of re-indexing every event — and can never answer
differently from a full replay.

Measured before snapshots: reopening cost ~145 µs an event (decode + tokenise + index), ~6½ CPU-minutes for
poster.place's 2.6M events at every restart and deploy. These tests hold the snapshot to the full replay:

  * a store reopened from its snapshot answers random filters EXACTLY like one rebuilt from the log, and
    replays nothing (`last_open["replayed"] == 0` — the CPU saved, measured without a clock);
  * writes after a snapshot-reopen follow every rule (replaceable versions, deletions of earlier events, the
    deleted-by-author refusal) — the reloaded indexes are live, not a read-only picture;
  * a crash after the snapshot loses nothing: the log tail written after it is replayed on top;
  * a snapshot that does not match is NEVER used: after a compaction, after a tokenizer change, corrupt or cut
    short — the store falls back to the full replay and still answers correctly;
  * a stop with nothing new written does not rewrite it (SSD writes).
"""
import os
import random
import time

import pytest

from app.services.posterchandb import snapshot as snap
from app.services.posterchandb.store import Store

pytest.importorskip("psycopg2")
from tests.test_posterchandb_vs_relay import Gen   # noqa: E402  (the same event and filter generator)


def _fill(path, seed, n=1500, **kw):
    s = Store(path, flush_interval=3600, direct_durable=False, snapshot_min_events=1, **kw)
    g = Gen(seed)
    for i in range(n):
        s.put(g.event(), origin=g.r.choice(["direct", "wot", "bridge"]))
        if i % 300 == 299:
            s.flush()
    return s, g


def _answers(store, gen, n=300, seed=7):
    gen.r = random.Random(seed)
    now = int(time.time())
    return [[e["id"] for e in store.query(gen.flt(), now=now)] for _ in range(n)]


def test_a_snapshot_reopen_answers_exactly_like_a_full_replay_and_replays_nothing(tmp_path):
    p = str(tmp_path / "db")
    s, g = _fill(p, 1)
    s.close()                                         # clean stop: writes the snapshot
    assert os.path.exists(os.path.join(p, snap.NAME))
    fast = Store(p)
    full = Store(p, snapshots=False)
    try:
        assert fast.last_open["snapshot"] is True and fast.last_open["replayed"] == 0, fast.last_open
        assert full.last_open["snapshot"] is False
        assert _answers(fast, g) == _answers(full, g)
    finally:
        full.close()
        fast.close()


def test_writes_after_a_snapshot_reopen_follow_every_rule(tmp_path):
    p = str(tmp_path / "db")
    s, g = _fill(p, 2)
    s.close()
    a = Store(p)
    b_path = str(tmp_path / "ref")
    ref, g2 = _fill(b_path, 2, snapshots=False)        # the same history, never snapshotted
    try:
        more = Gen(99)
        more.made = list(g.made)                          # new events may replace / delete the old ones
        more.authors = g.authors
        for _ in range(600):
            ev, origin = more.event(), more.r.choice(["direct", "wot"])
            assert a.put(dict(ev), origin=origin) == ref.put(dict(ev), origin=origin)
        assert _answers(a, g) == _answers(ref, g)
    finally:
        a.close()
        ref.close()


def test_a_crash_after_the_snapshot_recovers_the_tail_from_the_log(tmp_path):
    p = str(tmp_path / "db")
    s, g = _fill(p, 3)
    s.snapshot()
    for _ in range(400):
        s.put(g.event(), origin="wot")
    s.flush()
    s._file.close()                                    # the process dies: no close(), no new snapshot
    fast = Store(p)
    full = Store(p, snapshots=False)
    try:
        assert fast.last_open["snapshot"] and 0 < fast.last_open["replayed"] <= 400, fast.last_open
        assert _answers(fast, g) == _answers(full, g)
    finally:
        full.close()
        fast.close()


@pytest.mark.parametrize("how", ["compacted", "tokenizer", "corrupt", "truncated"])
def test_a_snapshot_that_does_not_match_is_never_used(tmp_path, monkeypatch, how):
    p = str(tmp_path / "db")
    s, g = _fill(p, 4, segment_bytes=60000)
    s.snapshot()
    if how == "compacted":
        s.kill([q for q in range(len(s.off)) if s.seg[q] == 1 and not s.dead[q]][5:])
        s.flush()
        assert s.compact(sid=1, pace=lambda *a: None)["compacted"] == 1
    s.flush()
    s._file.close()
    f = os.path.join(p, snap.NAME)
    if how == "tokenizer":
        monkeypatch.setattr(snap, "_fingerprint", lambda: "another-parser")
    elif how == "corrupt":
        with open(f, "r+b") as fh:
            fh.seek(os.path.getsize(f) // 2)
            fh.write(b"\x00" * 4096)
    elif how == "truncated":
        with open(f, "r+b") as fh:
            fh.truncate(os.path.getsize(f) // 3)
    fast = Store(p)
    full = Store(p, snapshots=False)
    try:
        assert fast.last_open["snapshot"] is False, (how, fast.last_open)
        assert _answers(fast, g) == _answers(full, g)
    finally:
        full.close()
        fast.close()


def test_a_stop_with_nothing_new_does_not_rewrite_the_snapshot(tmp_path):
    p = str(tmp_path / "db")
    s, _ = _fill(p, 5, n=300)
    s.close()
    f = os.path.join(p, snap.NAME)
    before = os.stat(f).st_mtime_ns
    again = Store(p, snapshot_min_events=1)
    again.close()                                      # nothing written in between
    assert os.stat(f).st_mtime_ns == before, "a restart rewrote an unchanged snapshot"


def test_maintenance_snapshots_after_a_compaction(tmp_path):
    from app.services.posterchandb import maintenance as M
    p = str(tmp_path / "db")
    s, _ = _fill(p, 6, segment_bytes=60000)
    s.snapshot()
    s.kill([q for q in range(len(s.off)) if s.seg[q] == 1 and not s.dead[q]])
    s.flush()
    th = M.Throttle(io_mb_s=1000, cpu_pct=100, busy_load=0, sleep=lambda x: None)
    m = M.Maintainer(s, lambda: M.Policy(min_free_pct=0), throttle=th)
    r = m.run_pass()
    assert r["compacted"] and r["snapshot"], r
    s.flush()
    s._file.close()
    again = Store(p)
    try:
        assert again.last_open["snapshot"] is True, again.last_open
    finally:
        again.close()
