"""A stored copy that fails its checksum must not be fetched for ever.

Reported after the checksum was restored and began doing its job: the same two videos failing on
every sweep, downloading real bytes each time.

Refusing the file was right; retrying it was not. The chunks are content-addressed, so reassembling
them is deterministic — the same stored copy yields the same wrong hash every time. The guard shipped
without anyone testing the SECOND attempt, which is this.

The block is keyed on the copy's IDENTITY, not the path, so it lifts by itself when the holder
publishes a different one. And the obvious repair — dropping the manifest entry so the holder
re-uploads — is a catastrophe this also guards: the device that has the file holds it in its own
`base`, so a vanished entry reads as "deleted elsewhere" and it trashes its only good copy.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "bad_copy_sim.js")


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


def test_a_bad_copy_is_refused_and_then_left_alone():
    """Mutation-verified: remove the skip and this reports 'it fetched the same broken copy again —
    that is the loop'."""
    got = _run()
    assert got["refusedFirstTime"] == 1, "the bad copy was written instead of refused"
    assert got["fetchesAfterSweepTwo"] == got["fetchesBeforeSweepTwo"], \
        "it re-fetched the same broken copy — the loop is back"
    assert got["saidSo"] == 1, "it went quiet instead of saying the file needs re-sending"


def test_the_manifest_entry_survives():
    """Dropping it would make the device that HAS the file delete its only copy."""
    assert _run()["entryKept"] is True


def test_a_repaired_copy_downloads_with_nobody_doing_anything():
    """The block is on the identity, so a re-upload clears it — no state to expire, no user action."""
    assert _run()["repairedDownloaded"] is True
