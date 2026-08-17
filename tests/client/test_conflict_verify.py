"""Settling a conflict must not pull the file into the renderer.

Reported from the device, and the most precise sentence of the whole investigation:

    "i see conflict 1/1927 then crash"

A folder whose agreement is empty but whose manifest is full conflicts on many paths at once, and
settling each one means asking "are these the same bytes". The only way the engine had was `fs.read`
— the whole file into the plugin, base64 across the bridge, then a hash pass in the renderer — bounded
by `_VERIFY_MAX`, which was TWO GIGABYTES. Generous on a desktop; meaningless on a phone. It died on
conflict number one.

Skipping the verify is not an alternative: it is what settles the conflict, and skipping 1,927 of them
duplicates the whole folder on every device. So the file is hashed where it lives (`fs.hashFile`,
streamed natively), and the whole-file read survives only as the fallback for a platform that cannot,
bounded by what that platform says it can hold.

The simulation is mutation-verified: restore `store.hashBytes(await fs.read(...))` and it reports both
failures at once — every file read whole, and a conflict copy for every one of them.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "conflict_verify_sim.js")


def _run(n, timeout=600):
    if shutil.which("node") is None:
        pytest.skip("no node")
    r = subprocess.run(["node", SIM, str(n)], capture_output=True, text=True, timeout=timeout)
    out = r.stdout.strip()
    assert out, "the simulation printed nothing:\n" + r.stderr[-3000:]
    got = json.loads(out[out.index("{"):])
    assert not got["failures"], "\n".join(got["failures"])
    assert r.returncode == 0, r.stderr[-2000:]
    return got


def test_1927_conflicts_settle_without_reading_a_single_file_whole():
    """The exact number from the device, because the count is what makes it fatal: one whole-file read
    is survivable, and 1,927 of them is not."""
    got = _run(1927)
    w = got["withNativeHash"]
    assert w["wholeFileReads"] == 0, "the crash is still there"
    assert w["nativeHashes"] >= 1927 * 0.9, "the adapter was not asked to hash"
    assert w["unsettled"] == 0, "conflicts left unsettled become a duplicate on every device"
    assert w["copies"] == 0
    assert w["settled"] >= 1927 * 0.9


def test_a_platform_that_cannot_hash_natively_still_settles_what_it_can_hold():
    """Desktop, and any APK older than `hashFile`. It must keep settling conflicts — refusing to read
    anything would duplicate the folder — but never read a file bigger than one chunk."""
    got = _run(200, timeout=300)
    f = got["fallback"]
    assert f["reads"] > 0, "it stopped settling conflicts altogether"
    assert f["oversizedReads"] == 0, "a file larger than one chunk was still read whole"


def test_a_first_sweep_does_not_hash_the_whole_folder_when_it_can_hash_on_demand():
    """WHY PAUSE APPEARED TO HANG. The scan is a single call into the platform and nothing can
    interrupt one in flight, so hashing every photo on the device before anything moves left
    "stopping…" on screen for as long as that took.

    The up-front hash exists to give the conflict check a local content identity. That check asks the
    adapter per file now, so hashing everything first is reading tens of gigabytes to answer a
    question about the few hundred paths that actually conflict. A platform WITHOUT a native hash must
    still do it — otherwise a joining device duplicates every file it already has — and that half is
    asserted too."""
    got = _run(1927)
    up = got["upFrontHash"]
    assert up["withNativeHash"] is False, "it still hashes the whole folder before it starts"
    assert up["without"] is True, "a platform with no native hash lost its only way to settle a join"
