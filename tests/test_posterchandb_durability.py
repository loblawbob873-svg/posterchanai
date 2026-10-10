"""PosterChanDB on the way down: a kill, a stop, and what reaches the disk.

"HOPE you have test cases for shutdown and syncing to disk". The store already had in-process crash tests (a
closed file handle standing in for a dead process). These use REAL processes and the REAL syscalls:

  * a writer SIGKILLed at a random moment loses nothing it had flushed, and the store reopens cleanly -- the
    torn tail of an unflushed write is cut off, never read as an event;
  * a flush fsyncs the segment, a new segment fsyncs its directory, and the mirror's CLEAN marker is fsynced
    with its directory -- data that only reached Python's file object is not on the disk;
  * the mirror's stop fits systemd's window: posterchanai-relay.service has TimeoutStopSec=10s, and a stop that
    overran it would be SIGKILLed, leave no CLEAN, and cost a full copy from Postgres at every deploy. The stop
    takes no snapshot (the slow part; maintenance takes them hourly) and still reopens without copying;
  * end to end on Postgres: a relay-shaped process (RelayStore + attached mirror) writing continuously gets
    SIGTERM, exits inside the budget, and the next start reopens WITHOUT copying and proves its count equal to
    Postgres -- so every write Postgres accepted before the stop is in the mirror after it.
"""
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
import uuid

import pytest

from app.services.posterchandb import mirror as M
from app.services.posterchandb import snapshot as snap
from app.services.posterchandb.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
STOP_BUDGET_S = 5.0          # half of systemd's 10 s: the relay does other work in its stop as well


def _events(seed, n):
    sys.path.insert(0, ROOT)
    from tests.test_posterchandb_vs_relay import Gen
    g = Gen(seed)
    out, ids = [], set()
    while len(out) < n:
        ev = g.event()
        # plain, unique events every rule stores -- the tests are about the disk, not the ingest rules (an
        # expired or fedi-only event is REFUSED, and counting it as "lost" would blame the disk for a rule)
        if (ev["kind"] in (1, 7, 6) and ev["created_at"] <= time.time() and ev["id"] not in ids
                and not any(t and t[0] in ("expiration", "client-mode") for t in ev["tags"])):
            ids.add(ev["id"])
            out.append(ev)
    return out


WRITER = textwrap.dedent('''
    import json, sys
    sys.path.insert(0, %(root)r)
    from app.services.posterchandb.store import Store
    st = Store(%(path)r, flush_interval=3600, direct_durable=False)
    evs = json.load(open(%(evs)r))
    for i, ev in enumerate(evs, 1):
        st.put(ev, origin="wot")
        if i %% 40 == 0:
            st.flush()
            print("FLUSHED", i, flush=True)
    print("DONE", flush=True)
    import time; time.sleep(60)
''')


@pytest.mark.parametrize("kill_after_flushes", [1, 4, 9])
def test_a_writer_killed_mid_stream_keeps_everything_it_had_flushed(tmp_path, kill_after_flushes):
    import json
    evs = _events(40 + kill_after_flushes, 600)
    (tmp_path / "evs.json").write_text(json.dumps(evs))
    path = str(tmp_path / "db")
    script = tmp_path / "w.py"
    script.write_text(WRITER % {"root": ROOT, "path": path, "evs": str(tmp_path / "evs.json")})
    proc = subprocess.Popen([PY, str(script)], stdout=subprocess.PIPE, text=True)
    flushed, seen = 0, 0
    try:
        for line in proc.stdout:
            if line.startswith("FLUSHED"):
                flushed = int(line.split()[1])
                seen += 1
                if seen >= kill_after_flushes:
                    os.kill(proc.pid, signal.SIGKILL)       # no close(), no atexit, mid-stream
                    break
    finally:
        proc.wait(10)
    st = Store(path)
    try:
        have = {e["id"] for e in st.query({"ids": [e["id"] for e in evs[:flushed]], "limit": 5000})}
        missing = [e["id"][:12] for e in evs[:flushed] if e["id"] not in have]
        assert not missing, "%d flushed events lost to a SIGKILL: %r" % (len(missing), missing[:3])
        assert len(st.off) >= flushed
        st.put(evs[-1], origin="wot")                          # and the store writes on after the crash
        st.flush()
    finally:
        st.close()


def test_flush_and_rotation_fsync_the_files_they_wrote(tmp_path, monkeypatch):
    synced = []
    real = os.fsync

    def rec(fd):
        try:
            synced.append(os.readlink("/proc/self/fd/%d" % fd))
        except OSError:
            synced.append("?")
        return real(fd)
    st = Store(str(tmp_path / "db"), flush_interval=3600, direct_durable=False, segment_bytes=40000)
    monkeypatch.setattr(os, "fsync", rec)
    try:
        for ev in _events(7, 300):
            st.put(ev, origin="wot")
            if len(synced) == 0 and len(st.off) == 20:
                st.flush()
                assert any(p.endswith(".log") for p in synced), "a flush did not fsync its segment: %r" % synced
        synced.clear()
        st.flush()                                   # crosses segment_bytes: rotation
        assert any(p.endswith(".log") for p in synced), synced
        segs = [n for n in os.listdir(st.path) if n.startswith("seg-")]
        assert len(segs) >= 2, segs
        assert any(p == os.path.realpath(st.path) for p in synced), "a new segment's directory entry was not fsynced"
    finally:
        monkeypatch.setattr(os, "fsync", real)
        st.close()


class _Done(threading.Thread):
    def __init__(self):
        super().__init__()
        self.start()

    def run(self):
        pass


def test_the_mirror_stop_fits_systemds_window_takes_no_snapshot_and_reopens_without_copying(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    path = str(tmp_path / "relay")
    m = M.Mirror(path, "serve", lambda: None, flush_interval=3600)
    st = Store(path, flush_interval=3600, direct_durable=False)
    evs = _events(9, 32000)
    for ev in evs[:30000]:                            # a store with real content to flush and close
        st.copy_put(ev, origin="wot")
    st.flush()
    for ev in evs[30000:]:                            # and writes still in RAM when the stop comes
        st.copy_put(ev, origin="wot")
    m.store = st
    m.state = "ready"
    m._thread = _Done()
    m._maint = M.maint_mod.Maintainer(st, lambda: M.maint_mod.Policy(min_free_pct=0), interval=0.05)
    slow = threading.Event()
    monkeypatch.setattr(m._maint, "run_pass", lambda: slow.wait(30))    # a pass that would not end by itself
    m._maint.start()
    time.sleep(0.2)
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(os.readlink("/proc/self/fd/%d" % fd)), real(fd))[1])
    t = time.monotonic()
    m.close(clean_token="clean:test")
    took = time.monotonic() - t
    monkeypatch.setattr(os, "fsync", real)
    slow.set()
    assert took < STOP_BUDGET_S, "the mirror's stop took %.1fs; systemd allows the whole relay 10s" % took
    assert not os.path.exists(os.path.join(path, snap.NAME)), "the stop wrote a snapshot (the slow part)"
    assert os.path.exists(os.path.join(path, M.CLEAN)), "a clean stop left no CLEAN: the next start would copy"
    assert any(p.endswith(M.CLEAN) for p in synced), "CLEAN was not fsynced"
    assert any(p == os.path.realpath(path) for p in synced), "the directory holding CLEAN was not fsynced"
    again = Store(path)
    try:
        assert len(again.off) == 32000, "events in RAM at the stop were lost"
    finally:
        again.close()


# ---------------------------------------------------------------- end to end: SIGTERM on Postgres
psycopg2 = pytest.importorskip("psycopg2")
from tests import scratch_postgres  # noqa: E402

RELAY_PROC = textwrap.dedent('''
    import asyncio, json, signal, sys, time, threading
    sys.path.insert(0, %(root)r)
    import psycopg2
    from app.services.nostr_relay import store as relay
    from app.services.posterchandb import mirror as M
    dsn = %(dsn)r
    rs = relay.RelayStore(dsn); rs.open(asyncio.new_event_loop())
    m = M.Mirror(%(path)r, "serve", lambda: psycopg2.connect(dsn), flush_interval=3600, maintenance=False)
    stop = threading.Event()
    def on_term(*_):
        stop.set()
    signal.signal(signal.SIGTERM, on_term)
    rs.attach_mirror(m)
    evs = json.load(open(%(evs)r))
    i = 0
    ready = False
    while not stop.is_set():                         # the relay keeps storing until systemd stops it
        if i < len(evs):
            rs._write_exec.submit(rs._add_event_sync, evs[i], "wot").result()
            i += 1
            if i == 50:
                print("WRITING", flush=True)
        if m.state == "ready" and i >= 200 and not ready:
            ready = True
            print("READY", i, flush=True)
    t = time.monotonic()
    m, token = rs.detach_mirror()                    # what thread.py's shutdown does, then store.close()
    m.close(clean_token=token)
    rs.close()
    print("STOPPED", i, round(time.monotonic() - t, 2), m.state, flush=True)
''')


def test_sigterm_on_a_writing_relay_keeps_every_accepted_write_and_reopens_without_copying(tmp_path):
    import json
    try:
        admin = psycopg2.connect(scratch_postgres.dsn(), connect_timeout=5)
    except Exception as e:      # noqa: BLE001
        pytest.skip("Postgres not reachable: %s" % e)
    admin.autocommit = True
    schema = "pcai_pcdb_term_" + uuid.uuid4().hex[:10]
    admin.cursor().execute(f'CREATE SCHEMA "{schema}"')
    dsn = scratch_postgres.dsn() + f" options=-csearch_path={schema}"
    path = str(tmp_path / "relay")
    (tmp_path / "evs.json").write_text(json.dumps(_events(77, 3000)))
    script = tmp_path / "relay_proc.py"
    script.write_text(RELAY_PROC % {"root": ROOT, "dsn": dsn, "path": path, "evs": str(tmp_path / "evs.json")})
    proc = subprocess.Popen([PY, str(script)], stdout=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            if line.startswith("READY"):
                break
        time.sleep(1.0)                                  # stop it while it is writing, not between writes
        t = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        out = proc.stdout.read()
        proc.wait(30)
        took = time.monotonic() - t
        assert proc.returncode == 0, out
        assert "STOPPED" in out, out
        assert took < STOP_BUDGET_S * 2, "the stop took %.1fs (systemd: 10s)" % took
        assert os.path.exists(os.path.join(path, M.CLEAN)), out

        import asyncio
        from app.services.nostr_relay import store as relay
        rs = relay.RelayStore(dsn)
        rs.open(asyncio.new_event_loop())
        m = M.Mirror(path, "serve", lambda: psycopg2.connect(dsn), flush_interval=3600, maintenance=False)
        rs.attach_mirror(m)
        end = time.time() + 60
        while m.state not in ("ready", "stale", "failed") and time.time() < end:
            time.sleep(0.05)
        try:
            assert m.counters["copied"] == 0, "a clean stop was not trusted: the restart copied Postgres again"
            assert m.state == "ready", m.stats()                # count EQUAL to Postgres: nothing accepted was lost
            assert m.pg_count == m.my_count and m.pg_count > 200, m.stats()
        finally:
            rs.mirror = None
            m.close()
            rs.close()
    finally:
        if proc.poll() is None:
            proc.kill()
        admin.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
