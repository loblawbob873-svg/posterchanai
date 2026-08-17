"""The whole journey, once, at the reported numbers — "I want it to work in 1 shot".

Every other simulation here proves ONE rule. This proves the sequence a person performs, from the
state they were actually in: a desktop holding 6,331 files, a shared manifest that is nothing but
tombstones because every other device erased the folder, and a local agreement that still says those
files were synced.

  1. the desktop sweeps       → nothing trashed, everything uploaded, the manifest goes live again
  2. it sweeps again          → quiet
  3. the phone pairs the name → downloads all of it, verified, byte for byte
  4. both sweep again         → quiet, and the two disks hold the same files

WHAT WRITING IT FOUND, after a whole evening of fixes that changed nothing: the delete verdict does
not come from the agreement at all. `deleteLocal` compares the file's mtime against the TOMBSTONE's,
so a folder erased today beats photos taken in April whatever the agreement says. Clearing the
agreement — in Stop syncing, at add time, and then inside the sweep — could never have fixed it, and
every single-rule test passed because each happened to use a local file newer than the delete.

So the deletions become uploads: every file is here, none is anywhere else, and the alternative is an
empty folder. They are marked as a republish, so the guard asks once before anything is sent.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "one_shot_sim.js")


def _run(n, timeout=900):
    if shutil.which("node") is None:
        pytest.skip("no node")
    r = subprocess.run(["node", SIM, str(n)], capture_output=True, text=True, timeout=timeout)
    out = r.stdout.strip()
    assert out, "the simulation printed nothing:\n" + r.stderr[-3000:]
    got = json.loads(out[out.index("{"):])
    assert not got["failures"], "\n".join(got["failures"])
    assert r.returncode == 0, r.stderr[-2000:]
    return got


def test_the_reported_case_end_to_end():
    """6,331 files, the exact number reported, through every step."""
    got = _run(6331)
    d, p = got["desktop"], got["phone"]
    assert d["trashed"] == 0, "it trashed files"
    assert d["uploaded"] == 6331, "it did not republish the folder"
    assert got["manifestLive"] == 6331, 'the folder would still show "0 files"'
    assert d["secondSweepQuiet"] is True, "the desktop kept working after it was in step"
    assert p["downloaded"] == 6331, "the phone did not receive the folder"
    assert p["missing"] == 0 and p["corrupt"] == 0, "files were lost or corrupted in transit"
    assert p["verified"] >= 6331, "the phone wrote files it had not checked"
    assert p["secondSweepQuiet"] is True, "the phone kept working after it was in step"


def test_a_small_folder_takes_the_same_journey():
    """The same sequence where nothing batches, so the rule is not incidentally load-bearing."""
    got = _run(60, timeout=300)
    assert got["desktop"]["trashed"] == 0
    assert got["desktop"]["uploaded"] == 60
    assert got["phone"]["corrupt"] == 0 and got["phone"]["missing"] == 0
