"""PosterChanDB maintenance: the relay's auto-clean rules, disk reclamation, the disk guard — and the
ceiling on what all of that may cost a RAID5 behind an LVM cache ("make sure the maintenance-automation
won't kill the CPU/DISK"). Every rule here is asserted on what a reader SEES afterwards (query results,
files on disk, a reopened store), not on internal flags.
"""
import builtins
import os
import time
from collections import namedtuple

import pytest

from app.services.nostr_relay import store as relay
from app.services.posterchandb import maintenance as M
from app.services.posterchandb.store import COMPACT_CHUNK, Store
from tests.test_posterchandb import hx, mk

NOW = int(time.time()) - 120          # real time: the relay refuses events from the future
DAY = 86400
OLD = NOW - 400 * DAY
DU = namedtuple("DU", "total used free")


def ids(store, flt=None):
    return {e["id"] for e in store.query(flt or {"limit": 5000}, now=NOW)}


class NoSleep:
    def __init__(self):
        self.calls = []

    def __call__(self, s):
        self.calls.append(s)


def maint(store, pol=None, du=None, **kw):
    th = M.Throttle(io_mb_s=8, cpu_pct=10, busy_load=0, sleep=NoSleep())
    return M.Maintainer(store, lambda: pol or M.Policy(), now=lambda: NOW, throttle=th,
                        disk_usage=du or (lambda: DU(100, 10, 90)), **kw)


@pytest.fixture
def db(tmp_path):
    s = Store(str(tmp_path / "db"), flush_interval=3600, direct_durable=False)
    yield s
    s.close()


# ---------------------------------------------------------------- the relay's rules, one by one
def test_expiry_deletes_except_the_kinds_that_must_never_expire(db):
    # stored while valid, then the clock passes their expiration: maintenance must KILL them (reclaimable)
    soon = NOW + 300
    gone = mk(kind=1, created_at=NOW - 10, tags=[["expiration", str(soon)]])
    later = mk(kind=1, created_at=NOW - 10, tags=[["expiration", str(soon + 999)]])
    note = mk(kind=30078, created_at=NOW - 10, tags=[["d", "pcai:note:1"], ["expiration", str(NOW - 1)]])
    repo = mk(kind=30617, created_at=NOW - 10, tags=[["d", "r"], ["expiration", str(NOW - 1)]])
    for e in (gone, later, note, repo):
        assert db.put(e, direct=True) == "stored"       # never-expire kinds drop the tag, so even NOW-1 stores
    m = maint(db)
    m.now = lambda: soon + 1
    m.run_pass()
    assert db.dead[db.seq_of(gone["id"])] == 1
    assert db.dead[db.seq_of(later["id"])] == 0
    assert db.dead[db.seq_of(note["id"])] == 0
    assert db.dead[db.seq_of(repo["id"])] == 0


def test_retired_kinds_go_whoever_wrote_them(db, monkeypatch):
    # ingest refuses them now (as the relay does); this is the cleaner for rows stored before that rule
    monkeypatch.setattr(relay, "_RETIRED_KINDS", ())
    r = mk(kind=40, created_at=NOW - 5)
    keep = mk(kind=1, created_at=NOW - 5)
    db.put(r, direct=True); db.put(keep, direct=True)
    monkeypatch.undo()
    assert db.put(mk(kind=40, created_at=NOW - 4), direct=True) == "retired"
    maint(db).run_pass()
    assert ids(db) == {keep["id"]}


def test_the_age_rule_spares_direct_writes_preserved_authors_subscribers_and_non_feed_kinds(db):
    local, paying = hx(), hx()
    synced_old = mk(kind=1, created_at=OLD)
    synced_new = mk(kind=1, created_at=NOW - DAY)
    direct_old = mk(kind=1, created_at=OLD)
    local_old = mk(kind=1, created_at=OLD, pubkey=local)
    sub_old = mk(kind=1, created_at=OLD, pubkey=paying)
    profile_old = mk(kind=0, created_at=OLD)
    git_comment = mk(kind=1111, created_at=OLD, tags=[["K", "1617"]])
    plain_comment = mk(kind=1111, created_at=OLD, tags=[["K", "1"]])
    for e in (synced_old, synced_new, local_old, sub_old, profile_old, git_comment, plain_comment):
        db.put(e, origin="wot")
    db.put(direct_old, direct=True)
    maint(db, M.Policy(retention_days=30, preserve_pubkeys=[local], subscribers=[paying])).run_pass()
    assert ids(db) == {synced_new["id"], direct_old["id"], local_old["id"], sub_old["id"],
                       profile_old["id"], git_comment["id"]}


def test_without_a_retention_setting_nothing_is_aged_out(db):
    e = mk(kind=1, created_at=OLD)
    db.put(e, origin="wot")
    maint(db).run_pass()
    assert ids(db) == {e["id"]}


def test_orphaned_zaps_go_but_a_zap_for_a_stored_post_or_a_profile_stays(db):
    post = mk(kind=1, created_at=NOW - DAY)
    dead_post = hx()
    orphan = mk(kind=9735, created_at=OLD, tags=[["e", dead_post], ["p", hx()]])
    kept = mk(kind=9735, created_at=OLD, tags=[["e", post["id"]]])
    profile_zap = mk(kind=9735, created_at=OLD, tags=[["p", hx()]])
    db.put(post, direct=True)
    for e in (orphan, kept, profile_zap):
        db.put(e, origin="wot")
    maint(db, M.Policy(retention_days=30)).run_pass()
    assert ids(db) == {post["id"], kept["id"], profile_zap["id"]}


def test_a_zap_whose_post_the_same_pass_aged_out_is_an_orphan_too(db):
    post = mk(kind=1, created_at=OLD)
    zap = mk(kind=9735, created_at=OLD, tags=[["e", post["id"]]])
    db.put(post, origin="wot"); db.put(zap, origin="wot")
    maint(db, M.Policy(retention_days=30)).run_pass()
    assert ids(db) == set()


def test_bridge_dm_ttl_is_bridge_only(db):
    bridged = mk(kind=1059, created_at=NOW - 5 * DAY)
    fresh = mk(kind=1059, created_at=NOW - 1 * DAY)
    mine = mk(kind=1059, created_at=NOW - 50 * DAY)
    db.put(bridged, origin="bridge"); db.put(fresh, origin="bridge"); db.put(mine, direct=True)
    maint(db).run_pass()
    assert ids(db) == {fresh["id"], mine["id"]}


def test_pay_to_stay_never_touches_a_direct_write_unless_on_and_the_ledger_was_read(db):
    payer, free = hx(), hx()
    a = mk(kind=1, created_at=OLD, pubkey=free)
    b = mk(kind=1, created_at=OLD, pubkey=payer)
    db.put(a, direct=True); db.put(b, direct=True)
    maint(db, M.Policy(free_retention_days=30, subscribers=[payer], tiered_ok=False)).run_pass()
    assert ids(db) == {a["id"], b["id"]}, "an unread ledger must delete nothing"
    maint(db, M.Policy(free_retention_days=30, subscribers=[payer], tiered_ok=True)).run_pass()
    assert ids(db) == {b["id"]}


def test_the_count_cap_trims_the_oldest_synced_feed_only(db):
    evs = [mk(kind=1, created_at=NOW - i * 100) for i in range(10)]
    for e in evs:
        db.put(e, origin="wot")
    direct = mk(kind=1, created_at=OLD)
    db.put(direct, direct=True)
    maint(db, M.Policy(max_events=5)).run_pass()
    assert ids(db) == {e["id"] for e in evs[:5]} | {direct["id"]}   # the newest 5 overall; a direct write is never capped


def test_auto_clean_survives_a_restart(tmp_path):
    p = str(tmp_path / "db")
    s = Store(p, flush_interval=3600, direct_durable=False)
    old = mk(kind=1, created_at=OLD)
    s.put(old, origin="wot")
    maint(s, M.Policy(retention_days=30)).run_pass()
    s.close()
    s = Store(p)
    assert s.query({"limit": 10}, now=NOW) == []
    s.close()


# ---------------------------------------------------------------- disk reclamation
def _fill(s, n, size=600, origin="wot"):
    out = []
    for i in range(n):
        e = mk(kind=1, created_at=NOW - i, content=hx(size))
        s.put(e, origin=origin)
        out.append(e)
        if i % 20 == 0:
            s.flush()
    s.flush()
    return out


def test_a_mostly_dead_segment_is_rewritten_and_its_file_and_ram_released(tmp_path):
    p = str(tmp_path / "db")
    s = Store(p, flush_interval=3600, direct_durable=False, segment_bytes=16384, compact_dead_pct=40)
    evs = _fill(s, 120)
    segs0 = sorted(os.listdir(p))
    assert len(segs0) > 3
    first = 1
    victims = [e for e in evs if s.seg[s.seq_of(e["id"])] == first]
    keep = victims[: len(victims) // 3]
    s.kill([s.seq_of(e["id"]) for e in victims[len(victims) // 3:]])
    s.flush()
    r = s.compact(pace=lambda *a: None)
    assert r["compacted"] == first
    assert "seg-000001.log" not in os.listdir(p)
    assert first not in s.arenas
    alive = {e["id"] for e in evs} - {e["id"] for e in victims[len(victims) // 3:]}
    assert ids(s) == alive
    assert all(s.get(s.seq_of(e["id"])) == e for e in keep)
    s.close()
    s = Store(p)
    assert ids(s) == alive
    s.close()


def test_compaction_reads_nothing_from_disk(tmp_path, monkeypatch):
    p = str(tmp_path / "db")
    s = Store(p, flush_interval=3600, direct_durable=False, segment_bytes=16384)
    evs = _fill(s, 80)
    s.kill([s.seq_of(e["id"]) for e in evs if s.seg[s.seq_of(e["id"])] == 1][1:])
    s.flush()
    real = builtins.open

    def guarded(f, mode="r", *a, **k):
        if "seg-" in str(f) and "r" in mode and "+" not in mode:
            raise AssertionError("compaction read a segment back from disk")
        return real(f, mode, *a, **k)
    monkeypatch.setattr(builtins, "open", guarded)
    assert s.compact(pace=lambda *a: None)["compacted"] == 1
    monkeypatch.setattr(builtins, "open", real)
    s.close()


def test_markers_are_carried_forward_so_a_compaction_never_brings_anything_back(tmp_path):
    p = str(tmp_path / "db")
    s = Store(p, flush_interval=3600, direct_durable=False, segment_bytes=16384)
    a = _fill(s, 30)                         # segment 1 (and maybe 2)
    target = a[0]
    s.add_derived_tag(a[1]["id"], "_quote_author", "q" * 64)
    _fill(s, 60)                             # several more segments
    s.kill([s.seq_of(target["id"])])          # OP_DEAD lands in a LATER segment than its target
    _fill(s, 60)
    marker_seg = max(sid for sid, ms in s.seg_markers.items() if ms)
    s.flush()
    # compact the segment holding the OP_DEAD (its target lives in segment 1): the marker must move on
    s.compact(sid=marker_seg, pace=lambda *a: None)
    # and the one holding the derived tag's record + the tag
    s.compact(sid=1, pace=lambda *a: None)
    s.close()
    s = Store(p)
    got = ids(s)
    assert target["id"] not in got, "an auto-cleaned event came back after compaction + restart"
    quoted = {e["id"] for e in s.query({"#p": ["q" * 64], "_include_quotes": True, "limit": 50}, now=NOW)}
    assert a[1]["id"] in quoted, "a derived tag was lost when its record moved"
    s.close()


def test_a_crash_between_copy_and_delete_loses_and_duplicates_nothing(tmp_path, monkeypatch):
    p = str(tmp_path / "db")
    s = Store(p, flush_interval=3600, direct_durable=False, segment_bytes=16384)
    evs = _fill(s, 80)
    s.kill([s.seq_of(e["id"]) for e in evs if s.seg[s.seq_of(e["id"])] == 1][2:])
    s.flush()
    expect = ids(s)
    monkeypatch.setattr(os, "remove", lambda f: (_ for _ in ()).throw(OSError("power cut")))
    with pytest.raises(OSError):
        s.compact(sid=1, pace=lambda *a: None)
    monkeypatch.undo()
    s._file.close()                          # the process died: no close(), no flush
    s = Store(p)
    got = s.query({"limit": 5000}, now=NOW)
    assert {e["id"] for e in got} == expect
    assert len(got) == len(expect), "a record copied by the interrupted compaction appears twice"
    s.close()


# ---------------------------------------------------------------- what it may cost
def test_the_throttle_holds_both_the_byte_rate_and_the_cpu_share():
    sl = NoSleep()
    t = M.Throttle(io_mb_s=8, cpu_pct=10, busy_load=0, sleep=sl)
    t(4 * 1024 * 1024, 0.0)
    t(0, 0.1)
    assert sl.calls[0] == pytest.approx(0.5)      # 4 MB at 8 MB/s
    assert sl.calls[1] == pytest.approx(0.9)      # 0.1 s of CPU at a 10% share


def test_it_waits_while_the_machine_is_busy_and_gives_up_waiting_eventually():
    sl = NoSleep()
    loads = iter([3.0, 2.0, 0.2])
    t = M.Throttle(io_mb_s=1000, cpu_pct=100, busy_load=0.75, busy_wait=5, sleep=sl, loadavg=lambda: next(loads))
    t(0, 0)
    assert sum(sl.calls) == pytest.approx(10)
    sl2 = NoSleep()
    t = M.Throttle(busy_load=0.75, busy_wait=5, max_busy_wait=20, sleep=sl2, loadavg=lambda: 9.0)
    t(0, 0)
    assert sum(sl2.calls) == pytest.approx(20), "a permanently busy box must still get maintained"


def test_compaction_writes_in_big_sequential_pieces_and_is_paced_between_them(tmp_path):
    p = str(tmp_path / "db")
    s = Store(p, flush_interval=3600, direct_durable=False, segment_bytes=10 * 1024 * 1024)
    for i in range(3000):                    # segment 1 fills past 10 MB and closes at a flush
        s.put(mk(kind=1, created_at=NOW - i, content=hx(6000)), origin="wot")
        if i % 200 == 199:
            s.flush()
    s.flush()
    assert s._active > 1, "segment 1 should have closed"
    s.segment_bytes = 64 * 1024 * 1024
    paced, writes = [], []
    real_drain = s._drain

    def spy(fsync):
        writes.append((len(s.arenas[s._active]) - s._flushed, fsync))
        return real_drain(fsync)
    s._drain = spy
    s.compact(sid=1, pace=lambda nbytes, cpu: paced.append(nbytes))
    assert len(paced) >= 2, "the whole segment went out in one unpaced burst"
    assert max(paced) <= COMPACT_CHUNK + 7000
    assert len([w for w in writes if w[1]]) == 1, "one fsync per compacted segment, not one per chunk"
    assert min(n for n, f in writes[:-1]) >= COMPACT_CHUNK // 2, "small writes are RAID5 read-modify-writes"
    s.close()


def test_a_prune_of_many_events_is_a_few_appends_not_a_write_each(db):
    for i in range(5000):
        db.put(mk(kind=1, created_at=OLD - i, content="x"), origin="wot")
    db.flush()
    size0 = db._file.tell()
    m = maint(db, M.Policy(retention_days=30))
    m.run_pass()
    db.flush()
    assert db.query({"limit": 10}, now=NOW) == []
    assert db._file.tell() - size0 <= 5000 * 48     # ~41 bytes of framed marker per event, nothing else


# ---------------------------------------------------------------- disk guard
def test_low_disk_drops_the_oldest_refetchable_copies_and_never_a_direct_write(tmp_path):
    p = str(tmp_path / "db")
    s = Store(p, flush_interval=3600, direct_durable=False, segment_bytes=16384)
    synced = _fill(s, 100)
    mine = [mk(kind=1, created_at=OLD - i) for i in range(5)]
    for e in mine:
        s.put(e, direct=True)
    s.flush()
    state = {"free": 1}

    def du():
        return DU(100, 100 - state["free"], state["free"])
    m = maint(s, M.Policy(min_free_pct=5), du=du)
    calls = {"n": 0}
    real = s.kill

    def kill(seqs):
        calls["n"] += 1
        if calls["n"] >= 3:
            state["free"] = 50
        return real(seqs)
    s.kill = kill
    removed = m.disk_guard(M.Policy(min_free_pct=5))
    got = ids(s)
    assert removed > 0
    assert {e["id"] for e in mine} <= got
    oldest = sorted(synced, key=lambda e: e["created_at"])[:removed]
    assert not ({e["id"] for e in oldest} & got), "the guard did not take the OLDEST copies first"
    s.close()


def test_the_maintenance_thread_starts_stops_and_never_raises_into_the_relay(db):
    logs = []
    m = M.Maintainer(db, lambda: (_ for _ in ()).throw(RuntimeError("settings unreadable")),
                     interval=0.01, log=logs.append)
    m.start()
    import time
    time.sleep(0.2)
    m.stop()
    assert any("maintenance pass failed" in l for l in logs)
    assert not m._thread.is_alive()


# ---------------------------------------------------------------- NIP-09, the relay's way, across compaction
def test_an_event_its_author_already_deleted_is_refused_when_it_arrives_late(db):
    pk = hx()
    e = mk(kind=1, pubkey=pk, created_at=NOW - 50)
    d = mk(kind=5, pubkey=pk, created_at=NOW - 10, tags=[["e", e["id"]]])
    assert db.put(d, origin="wot") == "stored"
    assert db.put(e, origin="wot") == "deleted"
    stranger = mk(kind=5, created_at=NOW - 10, tags=[["e", hx()]])
    db.put(stranger, origin="wot")
    other = mk(kind=1, created_at=NOW - 5)
    assert db.put(other, origin="wot") == "stored"


def test_a_deletion_never_removes_a_giftwrap_or_another_deletion(db):
    pk = hx()
    gw = mk(kind=1059, pubkey=pk, created_at=NOW - 50)
    d0 = mk(kind=5, pubkey=pk, created_at=NOW - 40, tags=[["e", hx()]])
    db.put(gw, origin="wot"); db.put(d0, origin="wot")
    db.put(mk(kind=5, pubkey=pk, created_at=NOW - 10, tags=[["e", gw["id"]], ["e", d0["id"]]]), origin="wot")
    assert {gw["id"], d0["id"]} <= ids(db)


def test_an_a_deletion_matches_the_d_tag_like_postgres_does(db):
    pk = hx()
    art = mk(kind=30023, pubkey=pk, created_at=NOW - 50, tags=[["d", "post"]])
    newer = mk(kind=30023, pubkey=pk, created_at=NOW - 1, tags=[["d", "other"]])
    db.put(art, origin="wot"); db.put(newer, origin="wot")
    db.put(mk(kind=5, pubkey=pk, created_at=NOW - 10, tags=[["a", "30023:%s:post" % pk]]), origin="wot")
    assert art["id"] not in ids(db) and newer["id"] in ids(db)


def test_compaction_moving_a_deletion_forward_does_not_kill_what_arrived_after_it(tmp_path):
    """A giftwrap the deletion names arrives AFTER it and is kept (kind-5 never deletes 1059). The
    deletion's segment is compacted, which moves the deletion past the giftwrap; on restart the giftwrap
    must still be there, and the deletion must still have removed what it removed."""
    p = str(tmp_path / "db")
    s = Store(p, flush_interval=3600, direct_durable=False, segment_bytes=16384)
    pk = hx()
    victim = mk(kind=1, pubkey=pk, created_at=NOW - 100)
    s.put(victim, origin="wot")
    gw = mk(kind=1059, pubkey=pk, created_at=NOW - 90)
    d = mk(kind=5, pubkey=pk, created_at=NOW - 80, tags=[["e", victim["id"]], ["e", gw["id"]]])
    s.put(d, origin="wot")
    s.flush()
    _fill(s, 60)
    s.put(gw, origin="wot")
    _fill(s, 60)
    s.flush()
    assert s.compact(sid=1, pace=lambda *a: None)["compacted"] == 1
    s.close()
    s = Store(p)
    got = ids(s)
    assert gw["id"] in got and d["id"] in got and victim["id"] not in got
    s.close()
