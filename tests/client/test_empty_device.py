"""A device that has nothing must not tell every other device that everything is gone.

Found live: a desktop mid-upload of 6,334 files, and a phone still sweeping the same pair after its
folder had been removed. A device with an AGREEMENT and an EMPTY scan plans `deleteRemote` for every
path — tombstones, the record by which other devices learn a file was deleted — and `massDelete()`
returns null for it, because that guard only ever covered the LOCAL side (trashing files on this
disk). Measured: 500 remote deletions, guard silent.

The phone would have marked the whole folder deleted for everyone while the desktop was still
uploading it, and the desktop's next sweep would then have read "deleted elsewhere" for files it
holds and offered to trash them — the same catastrophe one sweep later.

An empty scan is never evidence a folder was emptied. It is a folder that was removed, a SAF grant
that lapsed, a disk that was not mounted, a path that moved.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "empty_device_sim.js")


def _run(n=500):
    if shutil.which("node") is None:
        pytest.skip("no node")
    r = subprocess.run(["node", SIM, str(n)], capture_output=True, text=True, timeout=300)
    out = r.stdout.strip()
    assert out, "the simulation printed nothing:\n" + r.stderr[-3000:]
    got = json.loads(out[out.index("{"):])
    assert not got["failures"], "\n".join(got["failures"])
    assert r.returncode == 0, r.stderr[-2000:]
    return got


def test_an_empty_scan_never_erases_the_folder_for_everyone():
    got = _run()
    e = got["emptyDevice"]
    assert e["tombstoned"] == 0, "it marked files deleted for every device"
    assert e["liveLeft"] == 500, "the shared manifest lost entries"
    assert e["refused"], "it refused silently, so nobody could ever know why"


def test_an_ordinary_delete_still_travels():
    """The guard must not stop deletions working — that would be the same bug with the sign flipped,
    which is what the contacts sweep learned the hard way."""
    o = _run()["ordinaryDelete"]
    assert o["tombstoned"] == 3, "a normal delete of 3 files stopped propagating"
    assert o["refused"] is None
