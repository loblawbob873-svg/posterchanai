"""EVERY PACKAGE THE INSTALLER ASKS FOR MUST EXIST IN A REPO THE INSTALLER ENABLES.

`app-misc/brightnessctl` was in POSTERCHANOS_PACKAGES and is not in the Gentoo tree. That is not a
missing feature: `emerge` fails on an unknown atom, so it broke the build of the WHOLE profile — on
every fresh machine, at the one step nobody can skip — and the audit next door could not see it,
because that audit asks whether every program the shell RUNS has a package listed, never whether the
listed package is real.

The two failure directions are different and both are silent from the other's side:

  * a tool with no package  → the control does nothing on a fresh install (tests/test_posterchanos_profile.py)
  * a package with no ebuild → nothing installs at all (here)

WHICH REPOS COUNT IS THE WHOLE CHECK. The installer writes repos.conf for the Gentoo mirror and the
PosterChan overlay, and enables steam-overlay; it does NOT enable guru or any personal overlay. A
developer box usually has more than that, so consulting every repo present would let a package that
only exists in `guru` pass here and fail on every real install — the exact shape of the bug this
file exists for. So the repo list is the installer's, not this machine's.

SKIPPED, WITH A REASON, on a machine with no portage tree: this is one of the few checks that needs
real distribution data, and a check that skips silently is a check nobody notices has stopped.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SH = ROOT / "os/gentoo.sh"

# The repos os/gentoo.sh actually configures: the Gentoo mirror, the PosterChan overlay, and
# steam-overlay (enabled with `eselect repository enable steam-overlay`).
TARGET_REPOS = ("gentoo", "posterchan", "steam-overlay")

# THE POSTERCHAN OVERLAY IS READ FROM THIS REPO, NOT FROM /var/db/repos/posterchan.
#
# That directory is a CLONE of what scripts/publish_overlay.sh last pushed, so it is downstream of
# `os/overlay/` by definition: every newly added overlay package failed this check until somebody
# published, which reads as "this atom does not exist" for a package sitting right there in the
# tree.  Checking the source instead keeps the two real failures this file exists for — an atom in
# no Gentoo category, and an atom we forgot to write an ebuild for — and drops a false one that
# only measured publish timing.  The other two repos stay live: we do not author them.
OVERLAY = ROOT / "os/overlay"


def _repos() -> list[Path]:
    base = Path("/var/db/repos")
    out = []
    for name in TARGET_REPOS:
        if name == "posterchan" and OVERLAY.is_dir():
            out.append(OVERLAY)
        elif (base / name).is_dir():
            out.append(base / name)
    return out


def _packages() -> set[str]:
    src = SH.read_text(encoding="utf-8")
    found: set[str] = set()
    for var in ("BASE_PACKAGES", "POSTERCHANOS_PACKAGES"):
        m = re.search(r'%s="(.*?)"' % var, src, re.S)
        assert m, f"{var} moved or was renamed — this check is reading nothing"
        for token in m.group(1).replace("\\\n", " ").split():
            if "/" in token:
                # Version operators are legal in an emerge atom and are not part of the path.
                found.add(token.lstrip("<>=~"))
    return found


def test_the_package_lists_are_still_readable():
    """A regex that silently matches nothing would make every check below pass vacuously."""
    pkgs = _packages()
    assert len(pkgs) > 50, f"only found {len(pkgs)} packages — the lists moved"
    assert "gui-wm/wayfire" in pkgs, "the compositor is not in the list this check is reading"


def test_every_package_exists_in_a_repo_the_installer_enables():
    repos = _repos()
    if not repos:
        pytest.skip("no portage tree at /var/db/repos — this needs real distribution data")
    missing = []
    for atom in sorted(_packages()):
        category, name = atom.split("/", 1)
        if not any((repo / category / name).is_dir() for repo in repos):
            missing.append(atom)
    assert not missing, (
        "these atoms are in os/gentoo.sh but in none of %s, so `emerge` fails and NOTHING installs: %s"
        % ([r.name for r in repos], missing))


def test_this_check_can_fail():
    """MUTATION: brightnessctl is the real one that shipped. It must still be absent from the tree,
    or this check has quietly stopped being able to detect the thing it was written for."""
    repos = _repos()
    if not repos:
        pytest.skip("no portage tree at /var/db/repos — this needs real distribution data")
    assert not any((repo / "app-misc" / "brightnessctl").is_dir() for repo in repos), (
        "app-misc/brightnessctl now exists — pick another known-absent atom for this mutation")


def test_a_developers_extra_overlays_are_not_consulted():
    """The bug this file guards is a package that resolves HERE and nowhere else. Reading every repo
    on the machine would reintroduce it."""
    assert "guru" not in TARGET_REPOS
    src = SH.read_text(encoding="utf-8")
    for name in TARGET_REPOS:
        assert name in src, f"{name} is consulted here but os/gentoo.sh never configures it"


def test_brightnessctl_is_not_back_in_the_package_list():
    """It broke `emerge` for the whole profile. The backlight is handed to the `video` group by a
    udev rule instead — see tests/test_posterchanos_profile.py."""
    assert "brightnessctl" not in SH.read_text(encoding="utf-8").split("POSTERCHANOS_PACKAGES=")[1][:2000]


def test_every_overlay_package_is_in_a_category_portage_reads():
    """AN EBUILD IN AN UNLISTED CATEGORY IS INVISIBLE, AND NOTHING ANYWHERE SAYS SO.

    `profiles/categories` is the whole list of categories a repository has; portage does not scan
    for directories.  So adding `gui-apps/wlr-randr/` to the overlay and shipping it published a
    package that `emerge` then reported as nonexistent -- identical, from the installer's side, to
    never having written the ebuild.  The directory is there, the Manifest is right, and the atom
    does not resolve.
    """
    if not OVERLAY.is_dir():
        pytest.skip("no os/overlay in this checkout")
    listed = set((OVERLAY / "profiles/categories").read_text(encoding="utf-8").split())
    present = {
        child.name
        for child in OVERLAY.iterdir()
        if child.is_dir() and child.name not in {"profiles", "metadata", "licenses", "eclass"}
    }
    assert present <= listed, (
        "these overlay categories are not in profiles/categories, so portage cannot see any ebuild "
        "in them: %s" % sorted(present - listed))


def test_every_overlay_ebuild_has_a_manifest():
    """A package with no Manifest fails verification on the machine, not here.

    "VERIFY FAILED! Reason: Insufficient data for checksum verification" is what an operator sees;
    it names no package and reads like a corrupt download.  Anything with a SRC_URI needs one.
    """
    if not OVERLAY.is_dir():
        pytest.skip("no os/overlay in this checkout")
    missing = []
    for ebuild in OVERLAY.glob("*/*/*.ebuild"):
        if "SRC_URI" not in ebuild.read_text(encoding="utf-8"):
            continue                      # nothing is downloaded, so there is nothing to check
        if not (ebuild.parent / "Manifest").is_file():
            missing.append(str(ebuild.relative_to(OVERLAY)))
    # posterchan-desktop's Manifest is generated at publish time from the release it pins.
    missing = [m for m in missing if "posterchan-desktop" not in m]
    assert not missing, "overlay ebuilds that download sources but carry no Manifest: %s" % missing
