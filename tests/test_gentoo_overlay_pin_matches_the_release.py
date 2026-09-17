"""The pinned tarball is the one GitHub is actually serving.

`test_gentoo_overlay_pins_resolve.py` checks the SHAPE of the pin — right tag form, one DIST row,
both digests, the ebuild and the Manifest agreeing on a version. All of that can be perfectly
consistent and still name a file that does not exist, or a file whose bytes are not the ones that
were hashed, and the overlay has gone stale that way three times (1.0.818, 1.0.825, 1.0.1611). What
an operator then sees is "VERIFY FAILED" or a 404 during an install — on their machine, about a
package their own OS ships, long after the mistake was made.

So this asks GitHub. The release API reports each asset's exact SIZE, which is enough to catch both
"the version was bumped and the digest was not" and "the digest belongs to a different build" —
without downloading 150 MB in a test. Offline, or without `gh`, it SKIPS: a check that cannot reach
the network must not turn a working overlay red.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "os" / "overlay" / "app-misc" / "posterchan-desktop"


def _pinned():
    ebuilds = sorted(PKG.glob("posterchan-desktop-*.ebuild"))
    assert len(ebuilds) == 1, "expected exactly one desktop ebuild, found %s" % ebuilds
    version = ebuilds[0].name[len("posterchan-desktop-"):-len(".ebuild")]
    rows = [r for r in (PKG / "Manifest").read_text(encoding="utf-8").splitlines() if r.strip()]
    assert len(rows) == 1, rows
    parts = rows[0].split()
    return version, parts[1], int(parts[2])


def _release(tag):
    gh = shutil.which("gh")
    if not gh:
        pytest.skip("gh is not installed, so the release cannot be asked about")
    result = subprocess.run([gh, "release", "view", tag, "--json", "assets,isDraft"],
                            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        note = (result.stderr or "").strip()
        if "release not found" in note.lower():
            pytest.fail("the overlay pins %s and GitHub has no such release: %s" % (tag, note))
        pytest.skip("could not reach GitHub (%s)" % note[:120])
    return json.loads(result.stdout)


@pytest.mark.skipif(not PKG.is_dir(), reason="no overlay in this checkout")
def test_the_pinned_release_exists_and_carries_the_file_the_manifest_names():
    version, dist, size = _pinned()
    release = _release("desktop-v%s" % version)
    wanted = "PosterChan-%s-linux-x64.tar.zst" % version
    assets = {asset["name"]: asset for asset in release.get("assets", [])}
    assert wanted in assets, (
        "the overlay pins %s but that release carries %s" % (wanted, sorted(assets)))
    assert not release.get("isDraft"), (
        "a DRAFT release is invisible to portage's fetch — every install would 404")
    # THE SIZE IS THE CHEAP HALF OF THE DIGEST. A version bumped without re-hashing leaves the old
    # size behind, which is exactly the shape that shipped a broken overlay three times.
    assert assets[wanted]["size"] == size, (
        "the Manifest says %s is %d bytes; the published asset is %d. The digest was not "
        "recomputed for this version." % (dist, size, assets[wanted]["size"]))


@pytest.mark.skipif(not PKG.is_dir(), reason="no overlay in this checkout")
def test_the_manifest_names_the_version_in_the_ebuild_filename():
    """Cheap, offline, and the other half of the same mistake."""
    version, dist, _ = _pinned()
    assert dist == "posterchan-desktop-%s.tar.zst" % version
    src = (PKG / ("posterchan-desktop-%s.ebuild" % version)).read_text(encoding="utf-8")
    assert re.search(r'(?m)^SRC_URI=.*desktop-v\$\{PV\}/PosterChan-\$\{PV\}-linux-x64\.tar\.zst', src)


@pytest.mark.skipif(not PKG.is_dir(), reason="no overlay in this checkout")
def test_the_session_package_requires_a_desktop_new_enough_to_run_it():
    """The session and the app ship together. An ISO that installs a year-old desktop beside a new
    wayfire.ini is the shape where half the fixes are present and nothing says which half."""
    shell = sorted((ROOT / "os" / "overlay" / "app-misc" / "posterchanos-shell").glob("*.ebuild"))
    assert len(shell) == 1, shell
    depend = shell[0].read_text(encoding="utf-8")
    pin = re.search(r">=app-misc/posterchan-desktop-([0-9.]+)", depend)
    assert pin, "posterchanos-shell must require a minimum desktop version"
    version, _, _ = _pinned()
    assert pin.group(1) == version, (
        "the session requires desktop %s while the overlay publishes %s" % (pin.group(1), version))
