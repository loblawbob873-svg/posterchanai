"""A chunked upload carries a content checksum, so the far side can verify what it received.

Without it `verifyPart` returns early and a truncated or mis-assembled file is written to the other
device unchecked — reported as videos that synced and would not play. The checksum used to come from
the scan's up-front hash; that hash was removed (it reads tens of gigabytes to answer a question about
a few hundred paths, and it is what made Pause appear to hang), which quietly made "no checksum" the
normal state for every large file a phone uploads.

This does NOT repair a file already written badly — it makes the next bad transfer fail loudly
instead of silently.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "chunked_csum_sim.js")


def _run():
    if shutil.which("node") is None:
        pytest.skip("no node")
    r = subprocess.run(["node", SIM], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-2000:]
    out = r.stdout.strip()
    return json.loads(out[out.index("{"):])


def test_a_big_file_uploaded_in_chunks_is_verifiable_on_the_other_device():
    got = _run()
    assert got["withNativeHash"]["hasCsum"] is True, \
        "a chunked upload carries no checksum, so its download is never verified"
    assert got["withNativeHash"]["chunks"] > 1, "this did not take the chunked path at all"


def test_a_platform_that_cannot_hash_degrades_rather_than_failing():
    """Desktop, and any APK older than hashFile: the chunk list stays the identity, as before."""
    got = _run()
    assert got["withoutIt"]["hasCsum"] is False
    assert got["withoutIt"]["chunks"] > 1
