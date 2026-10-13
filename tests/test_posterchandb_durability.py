"""PosterChanDB on the way down: a kill, a stop, and what reaches the disk.

"HOPE you have test cases for shutdown and syncing to disk". The store already had in-process crash tests (a
closed file handle standing in for a dead process). These use REAL processes and the REAL syscalls:

  * a writer SIGKILLed at a random moment loses nothing it had flushed, and the store reopens cleanly -- the
    torn tail of an unflushed write is cut off, never read as an event;
  * a flush fsyncs the segment, and a new segment fsyncs its directory -- data that only reached Python's file
    object is not on the disk.
"""
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from app.services.posterchandb.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable


def _events(seed, n):
    sys.path.insert(0, ROOT)
    from tests.pcdb_gen import Gen
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
