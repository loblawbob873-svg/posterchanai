"""An overlay package whose files/ changed must get a new ebuild revision, or installed machines never see it.

2026-10-16: `posterchanos-shell/files/ptop` was fixed a day after the -r8 bump, with no -r9. `update-posterchan` runs
`emerge -u`, which does not reinstall an unchanged version, so every installed PosterChanOS machine kept the broken
ptop while a fresh ISO had the fix -- invisible from either side.

Rule, from git history: for each package directory, the newest commit touching `files/` must not be newer than the
commit that created the CURRENT ebuild file name. (A tree without git history -- a Docker build -- skips.)
"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "os/overlay"


def _git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=60)


def _have_history():
    r = _git("rev-parse", "--is-shallow-repository")
    return r.returncode == 0 and r.stdout.strip() == "false"


def _ts(*args) -> int:
    out = _git("log", "-1", "--format=%ct", *args).stdout.strip()
    return int(out) if out else 0


def _born(ebuild: Path) -> int:
    """When this ebuild FILE NAME first appeared. --full-history: path simplification otherwise hides an add made on
    a merged branch, and the check silently passes (it did, against the real -r8)."""
    # The oldest commit touching the path, merges included: an add that arrives by a merge has no diff of its own,
    # so --diff-filter=A cannot see it.
    out = _git("log", "--full-history", "--format=%ct", "--", str(ebuild.relative_to(ROOT))).stdout.split()
    return min(int(x) for x in out) if out else 0


def test_every_package_whose_files_changed_has_a_newer_revision():
    if not _have_history():
        pytest.skip("no full git history here")
    stale = []
    for files in sorted(OVERLAY.glob("*/*/files")):
        pkg = files.parent
        ebuilds = sorted(pkg.glob("*.ebuild"))
        if not ebuilds or any(e.name.endswith("-9999.ebuild") for e in ebuilds):
            continue
        rel_files = str(files.relative_to(ROOT))
        changed = _ts("--", rel_files)
        # When did the newest ebuild NAME come into being (added or renamed to)?
        born = max(_born(e) for e in ebuilds)
        if changed and born and changed > born:
            stale.append("%s: files/ changed after %s was created" % (pkg.relative_to(OVERLAY),
                                                                       max(ebuilds).name))
    assert not stale, "bump the ebuild revision so `emerge -u` reinstalls it:\n" + "\n".join(stale)
