"""A file that arrives must be the file that was sent — real bytes, phone to desktop.

WHY THIS EXISTS, and it is an indictment of the rest of this directory. Videos synced from a phone
and would not play, while 584 tests stayed green — because every one of them asserted the thing its
author had just fixed (counts, phases, plans, guards) and not one moved a byte and compared it. So
removing the up-front hash, which was also the only thing putting a content checksum on a large file,
went through the whole suite without a murmur and the receiving side quietly stopped verifying
anything.

The two devices use DIFFERENT chunk sizes (4 MB on the phone, 16 MB on the desktop), which is the
arrangement that actually exists and the one that makes a wrong reassembly possible.

Mutation-verified against the real regression: restore it and the file still arrives byte-identical,
but the run fails on "the far side verifies nothing it receives" — which is the failure that shipped.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "roundtrip_integrity_sim.js")


def _run():
    if shutil.which("node") is None:
        pytest.skip("no node")
    r = subprocess.run(["node", SIM], capture_output=True, text=True, timeout=600)
    out = r.stdout.strip()
    assert out, "the simulation printed nothing:\n" + r.stderr[-3000:]
    got = json.loads(out[out.index("{"):])
    assert not got["failures"], "\n".join(got["failures"])
    assert r.returncode == 0, r.stderr[-2000:]
    return got


def test_a_40mb_video_survives_the_round_trip_byte_for_byte():
    got = _run()
    assert got["videoBytes"] == got["expected"], "it arrived truncated"
    assert got["identical"] is True, \
        "same length, different bytes — a chunk written at the wrong offset looks exactly like this"


def test_the_receiving_device_could_actually_check_what_it_received():
    """The half that went missing. A correct file nobody could have verified is a silent failure
    waiting to happen, and today it happened."""
    got = _run()
    assert got["entryHasCsum"] is True, \
        "a chunked upload carries no content identity, so nothing verifies the download"
    assert got["verifiedOnArrival"] >= 1, "the file was put in place without being hashed"
