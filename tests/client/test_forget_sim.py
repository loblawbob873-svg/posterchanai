"""Forgetting a folder actually empties the record — the outcome, verified against the real merge.

Reported as "how can 8K always be removed": pressing forget said "8,132 entries cleared" every time
and the folder never went away. It never cleared anything.

`save()` re-reads and merges whenever it is given a `touched` list, and the merge writes a path only
`if(paths[p] !== undefined)` — a missing key means "leave it alone", which is why every deletion in
this feature is a tombstone rather than a removed key. `forget` passed an EMPTY manifest plus all
8,132 paths, so every lookup was undefined, every assignment skipped, and the document was written
back unchanged. The POST succeeded, so it claimed success.

The simulation's stub implements that merge EXACTLY, because a stub that stored whatever it was
handed would have passed against the broken code — which is how this shipped in the first place.
Mutation-verified: put `touched` back and the run reports "the record still holds 8133 entries".
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "forget_sim.js")


def _run():
    if shutil.which("node") is None:
        pytest.skip("no node")
    r = subprocess.run(["node", SIM], capture_output=True, text=True, timeout=300)
    out = r.stdout.strip()
    assert out, "the simulation printed nothing:\n" + r.stderr[-3000:]
    got = json.loads(out[out.index("{"):])
    assert not got["failures"], "\n".join(got["failures"])
    assert r.returncode == 0, r.stderr[-2000:]
    return got


def test_the_record_is_actually_empty_afterwards():
    got = _run()
    assert got["entriesBefore"] > 8000, "the fixture is not at the reported scale"
    assert got["entriesAfter"] == 0, "the shared record survived being forgotten"


def test_a_second_press_reports_nothing_left():
    """The tell that it was never working: it said 8,132 cleared every single time."""
    assert _run()["secondPress"] == 0


def test_live_files_and_tombstones_are_counted_separately():
    """"8,132 entries cleared" under a folder showing "0 files" reads as a contradiction; the two
    numbers are different things and the tombstones are why the folder was stuck."""
    got = _run()
    assert got["live"] == 1
    assert got["tombstones"] == got["entriesBefore"] - 1


def test_it_clears_every_devices_record_not_just_this_ones():
    """The mechanism behind the bug, in its current form.

    Under one shared document the failure was `touched`: a merge cannot express "remove this key",
    so a wipe that passed one changed nothing while reporting 8,132 entries cleared. Under one
    document PER DEVICE the same shape is different — clearing only our own leaves the others, and
    the folder returns the moment one of them publishes again. So the sim holds two devices, and the
    counts below are the merged record after the wipe."""
    got = _run()
    assert got["devices"] == 2, got
    assert got["entriesAfter"] == 0, got
    assert got["secondPress"] == 0, got
