"""One PosterChanDB directory, one open Store -- a second opener is refused, never let in to write.

2026-10-16 09:36:50: something that was not the relay opened server1's LIVE store directory, imported its index, wrote a
checkpoint and run files into it and deleted index.snap underneath the running relay. Nothing could stop it: the store
took no lock. The second writer now gets StoreBusy, from another process as much as this one, and the lock goes with
the holder (a crashed relay leaves nothing stale).
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _child_open(path):
    code = ("import sys; sys.path.insert(0, %r)\n"
            "from app.services.posterchandb.store import Store, StoreBusy\n"
            "try:\n    s = Store(%r, flush_interval=3600); s.close(); print('OPENED')\n"
            "except StoreBusy as e:\n    print('BUSY', e)\n") % (ROOT, path)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120).stdout


def test_a_second_process_cannot_open_a_store_that_is_open(tmp_path):
    from app.services.posterchandb.store import Store
    d = str(tmp_path / "relay")
    st = Store(d, flush_interval=3600)
    try:
        before = sorted(os.listdir(d))
        out = _child_open(d)
        assert out.startswith("BUSY") and str(os.getpid()) in out, out
        assert sorted(os.listdir(d)) == before            # it wrote nothing on its way out
    finally:
        st.close()
    assert _child_open(d).startswith("OPENED")            # closed -> the next one gets in


def test_a_second_store_in_the_same_process_is_refused_too(tmp_path):
    from app.services.posterchandb.store import Store, StoreBusy
    d = str(tmp_path / "relay")
    st = Store(d, flush_interval=3600)
    try:
        with pytest.raises(StoreBusy):
            Store(d, flush_interval=3600)
    finally:
        st.close()
    Store(d, flush_interval=3600).close()


def test_a_crashed_holder_leaves_nothing_stale(tmp_path):
    d = str(tmp_path / "relay")
    code = ("import os, sys; sys.path.insert(0, %r)\n"
            "from app.services.posterchandb.store import Store\n"
            "Store(%r, flush_interval=3600); os._exit(9)\n") % (ROOT, d)
    assert subprocess.run([sys.executable, "-c", code], timeout=120).returncode == 9
    assert _child_open(d).startswith("OPENED")
