"""A sweep that would keep nothing is a broken agreement, not a delete order.

Reported repeatedly, on a folder whose files were all present and correct:

    "Pictures — move 6331 files on this device to the trash? ... this sweep keeps only 0."

Every rule fired correctly to produce that. The local agreement said those files had been synced, the
manifest said they were deleted elsewhere, so: delete here. The agreement had outlived the history it
described, and clearing it by hand did not stick — remove and re-add, and the dialog came back.

There is no legitimate reading of "act on this and the folder is empty". The agreement is discarded
and the comparison made again without one, which can only produce uploads, because a deletion
REQUIRES an agreement. It is the same direction the engine already leans (delete loses to edit) and
the recoverable one: wrong this way costs one more delete, wrong the other way costs the folder.

Bounded so it cannot swallow ordinary work — that half is asserted too.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "poisoned_base_sim.js")


def _run(n=300):
    if shutil.which("node") is None:
        pytest.skip("no node")
    r = subprocess.run(["node", SIM, str(n)], capture_output=True, text=True, timeout=300)
    out = r.stdout.strip()
    assert out, "the simulation printed nothing:\n" + r.stderr[-3000:]
    got = json.loads(out[out.index("{"):])
    assert not got["failures"], "\n".join(got["failures"])
    assert r.returncode == 0, r.stderr[-2000:]
    return got


def test_the_reported_state_uploads_instead_of_trashing():
    got = _run()
    p = got["poisoned"]
    assert p["trashed"] == 0, "it still moved files to the trash"
    assert p["askedToTrash"] is False, "it still offered to trash the folder"
    assert p["discarded"] >= 300, "the agreement was not discarded"
    assert p["uploaded"] == 300, "the files were not republished"


def test_it_stays_fixed_on_the_next_sweep():
    """The loop is the actual complaint — the dialog came back every time."""
    got = _run()
    assert got["secondSweep"]["askedToTrash"] is False
    assert got["secondSweep"]["uploads"] == 0


def test_an_ordinary_mass_delete_is_still_asked_about():
    """The rule needs a real number of deletions AND nothing kept. A folder partly deleted elsewhere
    is ordinary work, and swallowing it would be a worse bug than the one this fixes."""
    got = _run()
    assert got["ordinaryPartialDelete"]["discarded"] == 0, "the rule is too broad"
    assert got["ordinaryPartialDelete"]["askedToTrash"] is True, \
        "a real mass delete stopped being asked about"
