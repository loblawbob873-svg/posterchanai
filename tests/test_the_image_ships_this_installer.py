"""THE ISO MUST CARRY THE INSTALLER THAT BUILT IT, AND FOR TWO WEEKS IT CARRIED SEPTEMBER'S.

Measured 2026-09-17 on a LiveUSB that could not install itself:

    /usr/local/share/posterchanos/gentoo.sh   4070 lines  222146 bytes   Sep  4 16:30
    /usr/bin/gentoo.sh INSIDE THE IMAGE       4070 lines  222146 bytes   mtime Sep 17 21:38
    the checkout the ISO was built from       5310 lines  300925 bytes   Sep 17

`liveCD()` already carried a comment titled "THE IMAGE CARRIES THE INSTALLER THAT BUILT IT", written
after this same bug bit once before ("a live-medium bug fixed in the repo, rebuilt, and still broken on
the ISO").  The code under it resolved `$PCOS_TREE/gentoo.sh` — and `$PCOS_TREE` is chosen at the top
of the script by looking for a directory containing `bin/`, which on the build host is
`/usr/local/share/posterchanos`, written once at install time and never updated.  So the line meant to
guarantee freshness is precisely what pinned the image to install day.

WHAT IT COST, and why no existing test could see it.  The shipped scan predated the union-enumeration
fix, so it could not see the hybrid ISO's hfsplus partition — the one carrying `boot/` and `LiveOS/` —
and every install died on "No kernel found on this live medium" before asking about disks at all.
`tests/test_live_medium_scan_finds_every_medium.py` extracts that scan from `os/gentoo.sh` and proves
it works; its docstring even says the bug "has been fixed TWICE, one medium apart".  It was fixed twice
in a file the image does not ship.  **A test of the repository is not a test of the image**, and this
file is the missing half: it pins the RULE that the packed installer is the running script.
"""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENTOO_PATH = ROOT / "os/gentoo.sh"
GENTOO = GENTOO_PATH.read_text(encoding="utf-8")


class TheImageShipsThisInstaller(unittest.TestCase):
    def test_the_packed_installer_is_resolved_from_the_running_script(self):
        """`pseudoput usr/bin/gentoo.sh` must take its bytes from BASH_SOURCE, not from a tree."""
        m = re.search(r'pseudoput "usr/bin/gentoo\.sh".*?cat "\$(\w+)"', GENTOO)
        self.assertIsNotNone(m, "the image no longer installs /usr/bin/gentoo.sh by pseudo-file")
        var = m.group(1)
        # The assignment chain for that variable must consult the running script FIRST.
        block = GENTOO[:m.start()]
        assign = block.rindex("local %s=" % var)
        chain = block[assign:]
        self.assertIn("BASH_SOURCE", chain,
                      "the packed installer is not resolved from the running script — this is the "
                      "$PCOS_TREE bug that shipped a two-week-old installer")
        first = re.search(r"BASH_SOURCE|PCOS_TREE|/usr/local/share/posterchanos|/usr/bin/gentoo\.sh",
                          chain)
        self.assertEqual("BASH_SOURCE", first.group(0)[:11],
                         "a stale candidate is consulted before the running script")

    def test_a_stale_installed_tree_never_wins(self):
        """RUN the resolution with a stale $PCOS_TREE present and prove which file it picks.

        This is the shape that actually shipped: an installed tree that exists, is readable, and is
        old. A grep cannot tell "consults BASH_SOURCE" from "consults it second".
        """
        chain = re.search(
            r'(local LIVE_INSTALLER=""\n(?:.*\n)*?)\s*# AND IT SAYS WHICH ONE', GENTOO)
        self.assertIsNotNone(chain, "the LIVE_INSTALLER resolution block moved or was rewritten")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            running = tmp / "checkout" / "gentoo.sh"
            running.parent.mkdir()
            running.write_text("#!/bin/bash\n# CURRENT\n")
            stale_tree = tmp / "installed"
            (stale_tree / "bin").mkdir(parents=True)
            (stale_tree / "gentoo.sh").write_text("#!/bin/bash\n# SEPTEMBER 4\n")
            script = (
                "set -u\n"
                f'PCOS_TREE="{stale_tree}"\n'
                f'LOG=/dev/null\n'
                + chain.group(1).replace("local ", "")
                + '\necho "PICKED=$LIVE_INSTALLER"\n')
            # RUN IT AS A REAL SCRIPT, not `bash -c`: BASH_SOURCE is empty for a command string, so
            # -c would test the fallback path and call it a pass. `set -u` is on deliberately — an
            # unset BASH_SOURCE must not abort the build (it did, and this fixture caught it).
            resolver = running.parent / "resolve.sh"
            resolver.write_text(script)
            r = subprocess.run(["bash", str(resolver)], capture_output=True, text=True, timeout=30)
            picked = re.search(r"PICKED=(\S*)", r.stdout)
            self.assertIsNotNone(picked, r.stdout + r.stderr)
            self.assertNotEqual(str(stale_tree / "gentoo.sh"), picked.group(1),
                                "the stale installed tree was packed into the image")

    def test_the_build_refuses_when_the_overlay_disagrees(self):
        """An ISO and an `update-posterchan` must not hand out two different installers."""
        self.assertRegex(
            GENTOO,
            r"published overlay's gentoo\.sh .*is not the installer building this image",
            "nothing refuses to pack when the published overlay carries a different installer")
        # The comparison must be on CONTENT. A line-count or mtime test passes while a one-line
        # difference in the medium scan ships.
        gate = GENTOO[GENTOO.index("RUNNING_INSTALLER="):]
        gate = gate[:gate.index("Steam on this build host")] if "Steam on this build host" in gate else gate
        self.assertIn("cmp -s", gate, "the installer gate does not compare file contents")

    def test_the_medium_scan_fix_is_in_the_file_that_gets_packed(self):
        """The concrete regression: the union enumeration must be in os/gentoo.sh, which is now the
        file the image takes. Before the fix above, this was true and the ISO was still broken."""
        self.assertIn('lsblk -pnro NAME', GENTOO)
        self.assertIn('blkid -o device', GENTOO)
        scan = GENTOO[GENTOO.index('cand="$(printf'):]
        self.assertIn("awk 'NF && !seen[$0]++'", scan,
                      "the candidate list is no longer the union of both enumerations")


if __name__ == "__main__":
    unittest.main()
