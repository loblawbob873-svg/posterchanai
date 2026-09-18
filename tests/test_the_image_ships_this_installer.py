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
        """`pseudoput usr/bin/gentoo.sh` takes its bytes from the ONE resolver, which reads
        BASH_SOURCE first."""
        m = re.search(r'pseudoput "usr/bin/gentoo\.sh".*?cat "\$(\w+)"', GENTOO)
        self.assertIsNotNone(m, "the image no longer installs /usr/bin/gentoo.sh by pseudo-file")
        var = m.group(1)
        block = GENTOO[:m.start()]
        assign = block.rindex("%s=" % var)
        self.assertIn("pc_canonical_installer", block[assign:assign + 200],
                      "the packed installer is not resolved by pc_canonical_installer")
        fn = GENTOO[GENTOO.index("pc_canonical_installer() {"):]
        fn = fn[:fn.index("\n}\n") + 3]
        first = re.search(r"BASH_SOURCE|PCOS_TREE|/usr/local/share/posterchanos|/usr/bin/gentoo\.sh",
                          fn)
        self.assertEqual("BASH_SOURCE", first.group(0)[:11],
                         "a stale candidate is consulted before the running script")

    def test_a_stale_installed_tree_never_wins(self):
        """RUN the resolver with a stale $PCOS_TREE present and prove which file it picks.

        This is the shape that actually shipped: an installed tree that exists, is readable, and is
        old. A grep cannot tell "consults BASH_SOURCE" from "consults it second".
        """
        fn = GENTOO[GENTOO.index("pc_canonical_installer() {"):]
        fn = fn[:fn.index("\n}\n") + 3]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            running = tmp / "checkout" / "gentoo.sh"
            running.parent.mkdir()
            running.write_text("#!/bin/bash\n# CURRENT\n")
            stale = tmp / "installed"
            (stale / "bin").mkdir(parents=True)
            (stale / "gentoo.sh").write_text("#!/bin/bash\n# SEPTEMBER 4\n")
            script = ("set -u\n" + f'PCOS_TREE="{stale}"\n' + fn
                      + '\necho "PICKED=$(pc_canonical_installer)"\n')
            resolver = running.parent / "resolve.sh"
            resolver.write_text(script)
            r = subprocess.run(["bash", str(resolver)], capture_output=True, text=True, timeout=30)
            picked = re.search(r"PICKED=(\S*)", r.stdout)
            self.assertIsNotNone(picked, r.stdout + r.stderr)
            self.assertNotEqual(str(stale / "gentoo.sh"), picked.group(1),
                                "the stale installed tree won")
            self.assertEqual(str(resolver), picked.group(1),
                             "the resolver did not return the running script")

    def test_every_copy_into_the_installed_system_uses_the_same_resolver(self):
        """THE HALF THAT WAS MISSED, AND IT COST A WHOLE ISO BUILD.

        Fixing only the copy the LIVE IMAGE packs left the installer still seeding the TARGET from
        `$PCOS_TREE/gentoo.sh`. `bootloader()` runs inside that chroot (see the `TARGET=/` case at the
        top of gentoo.sh), so the install executed September's installer: the EFI boot entry was never
        written, `check_livecd_install_vm.py` measured zero PosterChanOS entries in the guest's NVRAM,
        and every installed machine inherited a two-week-old repair tool.
        """
        for m in re.finditer(r'cp -f "\$INSTALLER_SRC" "\$TARGET/usr/bin/gentoo\.sh"', GENTOO):
            before = GENTOO[:m.start()]
            assign = before.rindex("INSTALLER_SRC=")
            self.assertIn("pc_canonical_installer", before[assign:assign + 160],
                          "a copy into the installed system still resolves through $PCOS_TREE; that "
                          "is the file a chroot later EXECUTES")
        self.assertNotIn('INSTALLER_SRC="$PCOS_TREE/gentoo.sh"', GENTOO,
                         "an install-day tree is still being used as an installer source")

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
