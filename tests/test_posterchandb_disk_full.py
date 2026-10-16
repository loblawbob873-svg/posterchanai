"""PosterChanDB survives a FULL DISK: a failed write leaves the log exactly as it was before it.

"we need to make sure our relay database can grow to hundreds of GB's without any issues with writing to disk"
(2026-10-16). A write that fails with ENOSPC part-way has already put some of its bytes in the segment, and the
store did not move its write position -- so once space came back, every later record was appended AFTER that
half record. On the next open the CRC check stops at the half record and cuts the tail: every event flushed
after the disk filled was silently lost (and if the segment had been closed meanwhile, it was no longer the
"last" one a torn tail is allowed in). Driven with a write that really puts half its bytes on disk, then fails.
"""
import errno
import os
import sys
import time

import pytest

from app.services.posterchandb.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _events(seed, n):
    sys.path.insert(0, ROOT)
    from tests.pcdb_gen import Gen
    g = Gen(seed)
    out, ids = [], set()
    while len(out) < n:
        ev = g.event()
        if (ev["kind"] in (1, 7, 6) and ev["created_at"] <= time.time() and ev["id"] not in ids
                and not any(t and t[0] in ("expiration", "client-mode") for t in ev["tags"])):
            ids.add(ev["id"])
            out.append(ev)
    return out


class _FullOnce:
    """The segment's file object, except that the next write puts HALF its bytes on disk and then fails."""
    def __init__(self, real):
        self.real, self.armed = real, True

    def write(self, data):
        if self.armed:
            self.armed = False
            b = bytes(data)
            os.write(self.real.fileno(), b[: len(b) // 2])       # half really reaches the file
            raise OSError(errno.ENOSPC, "No space left on device")
        return self.real.write(data)

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.mark.parametrize("rotate_after", [False, True])
def test_a_full_disk_mid_flush_loses_nothing_flushed_afterwards(tmp_path, rotate_after):
    evs = _events(7, 120)
    st = Store(str(tmp_path), flush_interval=3600, direct_durable=False, segment_bytes=64 * 1024 if rotate_after else 256 * 1024 * 1024)
    for ev in evs[:40]:
        st.put(ev, origin="wot")
    st.flush()
    for ev in evs[40:60]:
        st.put(ev, origin="wot")
    st._file = _FullOnce(st._file)
    with pytest.raises(OSError):
        st.flush()                         # the disk is full: this flush fails
    st._file = st._file.real if isinstance(st._file, _FullOnce) else st._file
    for ev in evs[60:]:
        st.put(ev, origin="wot")
    st.flush()                             # space is back: everything pending is written
    st.close()

    st2 = Store(str(tmp_path), flush_interval=3600, direct_durable=False)
    have = {e["id"] for e in st2.query({"ids": [x["id"] for x in evs], "limit": 1000})}
    st2.close()
    lost = [i for i, e in enumerate(evs) if e["id"] not in have]
    assert not lost, ("events flushed after the disk filled were lost on reopen", lost[:10], len(lost))
