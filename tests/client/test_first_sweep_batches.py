"""A first sweep of a real Pictures folder runs in bounded batches, and batching changes no decision.

WHY. The engine assembles the whole folder before it moves a byte — every path's metadata, the plan,
the manifest snapshot and the agreement, all live at once. At 15,790 files that kills the WebView's
render process the moment a sweep starts: the app disappears and stays in the recents list, because
the process never died, only its renderer, so nothing is thrown and nothing reaches any log. It was
confirmed on the device the only way it could be — pausing the folders made the app stable, and
unpausing killed it within seconds.

Six fixes were shipped at this from a description of it before the engine was ever RUN at the size
that breaks. This runs it.

WHAT MAKES BATCHING SAFE, and why it is restricted to a FIRST sweep: an empty `base` is what every
destructive decision needs to be absent. A delete requires base to hold a path the scan does not, so
a partial view cannot delete anything. A conflict is decided per path and is correct on the page
holding it. The one decision a partial view would get wrong is downloading a remote-only path — it
would fetch, and overwrite, a file merely sitting in a later page — so every batch but the last sees
only the part of the manifest its own paths appear in.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "first_sweep_sim.js")


def _run(files, page, timeout=900):
    if shutil.which("node") is None:
        pytest.skip("no node")
    r = subprocess.run(["node", SIM, str(files), str(page)],
                       capture_output=True, text=True, timeout=timeout)
    out = r.stdout.strip()
    assert out, "the simulation printed nothing:\n" + r.stderr[-3000:]
    got = json.loads(out[out.index("{"):])
    assert not got["failures"], "\n".join(got["failures"])
    assert r.returncode == 0, r.stderr[-2000:]
    return got


def test_a_15790_file_first_sweep_never_holds_the_folder_at_once():
    """The size that actually killed the app. Every assertion lives in the simulation so it can also
    be run by hand; this asserts the shape of the result on top."""
    got = _run(15790, 750)
    assert got["batches"] >= 20, "it did not batch at that size"
    assert got["biggestPage"] <= 750
    assert got["uploaded"] == 15790, "a file was dropped by the batching"
    assert got["agreed"] >= 15790, "the agreement did not cover the folder, so the next sweep repeats it"
    assert got["remoteOnlyFetched"] is True


def test_a_small_folder_still_works():
    """The batching must not need a big folder to be correct — most folders are small, and this is the
    path they take too."""
    got = _run(300, 750, timeout=300)
    assert got["uploaded"] == 300
    assert got["batches"] == 1


def test_an_interrupted_first_sweep_resumes_instead_of_starting_again():
    """The old behaviour agreed nothing until the whole folder was done, so a sweep that died — which
    is exactly what was happening — began again from file one every time. That is the difference
    between a folder that eventually syncs and one that never can."""
    got = _run(2000, 500, timeout=300)
    r = got["resume"]
    assert r["firstRun"] > 0, "the interrupted run agreed nothing"
    assert r["secondRun"] > 0, "the resumed run did nothing"
    assert r["total"] <= 2000 * 1.05, "the resume re-uploaded work already agreed: %s" % r
