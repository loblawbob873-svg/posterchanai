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
    """6,331 files, the exact number reported, through every step — plus three videos, so the run
    covers the chunked path as well as the whole-file one."""
    got = _run(6331)
    d, p = got["desktop"], got["phone"]
    total = 6331 + len(got["videos"])
    assert d["trashed"] == 0, "it trashed files"
    assert d["uploaded"] == total, "it did not send the whole folder"
    assert got["manifestLive"] == total, 'the folder would still show "0 files"'
    assert d["secondSweepQuiet"] is True, "the desktop kept working after it was in step"
    assert p["downloaded"] == total, "the phone did not receive the folder"
    assert p["missing"] == 0 and p["corrupt"] == 0, "files were lost or corrupted in transit"
    assert p["verified"] >= total, "the phone wrote files it had not checked"
    assert p["secondSweepQuiet"] is True, "the phone kept working after it was in step"


def test_a_small_folder_takes_the_same_journey():
    """The same sequence where nothing batches, so the rule is not incidentally load-bearing."""
    got = _run(60, timeout=600)
    assert got["desktop"]["trashed"] == 0
    assert got["desktop"]["uploaded"] == 60 + len(got["videos"])
    assert got["phone"]["corrupt"] == 0 and got["phone"]["missing"] == 0


def test_videos_survive_the_round_trip_and_are_verifiable():
    """ITS OWN BUG, ITS OWN TEST. "Videos synced and would not play" was not the folder failing — it
    was a CHUNKED upload published with no checksum, so the receiving device wrote whatever arrived
    and nothing anywhere could tell. Small files were unaffected (they take the whole-file path,
    which always hashed), which is why it hid.

    Three real videos, over both devices' chunk sizes so they cannot take the whole-file path, with
    content that changes if a chunk lands at the wrong offset."""
    got = _run(60, timeout=600)
    assert got["videos"], "no videos in the run at all"
    for v in got["videos"]:
        assert v["chunked"] is True, "%s did not take the chunked path — proves nothing" % v["path"]
        assert v["hasCsum"] is True, \
            "%s was published with NO checksum: nothing can verify it on arrival" % v["path"]
        assert v["arrived"] is True, "%s never arrived" % v["path"]
        assert v["identical"] is True, \
            "%s arrived CORRUPT — this is the unplayable-video bug" % v["path"]
        assert v["verifiedBeforeWrite"] is True, \
            "%s was written without being hashed first" % v["path"]


def test_a_video_is_actually_big_enough_to_span_chunks():
    """Otherwise the test above could pass on a 'video' that fits in one chunk and never exercises
    reassembly, which is where a wrong offset shows up."""
    got = _run(60, timeout=600)
    for v in got["videos"]:
        assert v["bytes"] >= 20 * 1024 * 1024, "%s is only %s bytes" % (v["path"], v["bytes"])
