"""A change under a package's `files/` must come with a REVISION BUMP, or it ships to nobody.

Run: venv-unified/bin/python -m unittest tests.test_a_config_change_actually_reaches_the_machine

A Gentoo package's filename IS its version, and `emerge -uDN @world` — which is how every
PosterChanOS machine updates — decides what to reinstall by comparing versions. Edit a package's
`files/` under an unchanged ebuild filename and Portage sees nothing new: the change is in the repo,
in the overlay, in CI, and on no machine.

ONE PACKAGE IS ALREADY IMMUNE, AND IT IS THE ONE THAT TAUGHT US THIS. `scripts/publish_overlay.sh`
renames `app-misc/posterchanos-shell` to `1.0.<UTC timestamp>` on every publish, with a comment
saying why: "The shell used to stay 1.0.0 for ever, which is why installed machines said 'Already up
to date' while keeping an old launcher." So its committed version never moves and never needs to.

EVERY OTHER PACKAGE IN THE OVERLAY IS SHIPPED AT THE VERSION IN ITS FILENAME, and at least one has
already lost a fix that way: `gui-wm/wayfire`'s keyboard-removal patch was revised into its FILESDIR
fourteen minutes AFTER `-r1` was cut, and never re-released — so every machine satisfied
`>=gui-wm/wayfire-0.10.1-r1` with a build that predates the fix, for three days, with nothing
anywhere to say so. The diff is right, the overlay is right, `emerge` exits 0 having done exactly
what it was asked, and only the machine disagrees.

The exempt set is READ OUT OF THE PUBLISHER rather than listed here, so a package that gains or
loses timestamping cannot silently fall out of this check.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "os/overlay"


PUBLISHER = ROOT / "scripts/publish_overlay.sh"


def _timestamped_packages() -> set:
    """Packages whose version publish_overlay.sh rewrites on every publish.

    Read from the script, never typed here: a hand-kept copy of this list is the same kind of second
    source of truth that the rest of this file exists to catch.
    """
    text = PUBLISHER.read_text() if PUBLISHER.exists() else ""
    found = set()
    for match in re.finditer(r'mv "\$[A-Z_]+_EBUILD" "\$[A-Z_]+_DIR/([a-z0-9-]+)-\$\{', text):
        found.add(match.group(1))
    return found


def _git(*args) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, check=False).stdout.strip()


def _packages_with_files():
    """Every overlay package that installs content out of a `files/` directory."""
    for files_dir in sorted(OVERLAY.glob("*/*/files")):
        if files_dir.is_dir() and any(files_dir.iterdir()):
            yield files_dir.parent


class TheOverlayIsShaped(unittest.TestCase):
    """If these stop holding, the checks below are measuring nothing."""

    def test_there_are_packages_that_ship_files(self):
        pkgs = list(_packages_with_files())
        self.assertTrue(pkgs, "no overlay package ships a files/ directory — this suite is vacuous")

    def test_every_such_package_has_exactly_one_ebuild(self):
        for pkg in _packages_with_files():
            with self.subTest(package=pkg.name):
                self.assertEqual(len(list(pkg.glob("*.ebuild"))), 1,
                                 f"{pkg.name} has zero or several ebuilds; the revision is ambiguous")

    def test_git_history_is_readable(self):
        self.assertTrue(_git("rev-parse", "HEAD"), "no git history — the check below cannot run")


class AConfigChangeCarriesARevisionBump(unittest.TestCase):
    def test_the_exempt_set_is_real(self):
        """If the publisher stops timestamping the shell, this check must start covering it."""
        exempt = _timestamped_packages()
        self.assertIn("posterchanos-shell", exempt,
                      "publish_overlay.sh no longer timestamps the shell; it now needs revbumps")
        self.assertNotIn("wayfire", exempt,
                         "wayfire is timestamped now — this check would be enforcing a dead rule")

    def test_uncommitted_files_changes_come_with_a_bumped_ebuild(self):
        """The check that fires BEFORE the change ships, which is the only useful moment.

        A modified `files/` with the ebuild filename untouched is the exact shape that has shipped
        invisibly every time.
        """
        exempt = _timestamped_packages()
        for pkg in _packages_with_files():
            rel = pkg.relative_to(ROOT).as_posix()
            if pkg.name in exempt:
                continue
            touched_files = _git("status", "--porcelain", "--", f"{rel}/files")
            if not touched_files:
                continue
            # A revbump changes the ebuild's FILENAME, because in Gentoo the filename is the
            # version. Asking merely "did any ebuild change" is not the same question and does not
            # answer this one: editing RDEPEND inside the ebuild satisfies it while shipping the
            # same version, which is the bug. Compare the NAMES against HEAD.
            head = {line.rsplit("/", 1)[-1]
                    for line in _git("ls-tree", "--name-only", "HEAD", f"{rel}/").splitlines()
                    if line.endswith(".ebuild")}
            now = {f.name for f in pkg.glob("*.ebuild")}
            with self.subTest(package=pkg.name):
                self.assertNotEqual(
                    head, now,
                    f"{pkg.name}: files/ changed but the ebuild version did not.\n"
                    f"  changed: {touched_files}\n"
                    f"  ebuild still: {sorted(now)}\n"
                    f"  Rename {rel}/{pkg.name}-<ver>.ebuild to the next -rN, or `emerge -uDN "
                    f"@world` will reinstall nothing and this change reaches no machine.")

    def test_the_last_files_change_in_history_was_released(self):
        """And the same rule over committed history, so a bump skipped in a past commit is visible.

        Compares the commit that last touched `files/` with the commit that last changed the set of
        ebuild FILENAMES (which is what a revbump does). A files/ change newer than the last bump is
        sitting in the repo unreleased.
        """
        exempt = _timestamped_packages()
        for pkg in _packages_with_files():
            rel = pkg.relative_to(ROOT).as_posix()
            # A bump staged or pending in the working tree IS the release, and is the business of
            # the test above. Judging it from committed history alone would fail for the whole of
            # every fix — including the one being made right now.
            if pkg.name in exempt:
                continue
            if _git("status", "--porcelain", "--", f"{rel}/*.ebuild"):
                continue
            files_at = _git("log", "-1", "--format=%ct", "--", f"{rel}/files")
            bump_at = _git("log", "-1", "--format=%ct", "--diff-filter=AR", "--", f"{rel}/*.ebuild")
            if not files_at or not bump_at:
                continue
            with self.subTest(package=pkg.name):
                self.assertGreaterEqual(
                    int(bump_at), int(files_at),
                    f"{pkg.name}: the newest files/ change is older than no revision bump — it was "
                    f"committed without one and has never reached an installed machine.")


if __name__ == "__main__":
    unittest.main()
