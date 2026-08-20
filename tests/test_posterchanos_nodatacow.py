"""NODATACOW on the write-heavy paths — and the two ways the obvious version of this does nothing.

btrfs copy-on-write turns each small overwrite inside a big file into a new extent. A database, a VM
image and a browser profile all do exactly that, all day, and the file ends up in tens of thousands
of fragments. `chattr +C` is the fix, and the naive spelling of it is a no-op:

  1. +C ONLY TAKES ON A ZERO-LENGTH FILE. On a file that already has extents the ioctl is refused or
     accepted-and-ignored. `chattr -R +C <populated dir>` therefore reports success and converts
     nothing — the worst outcome, because it looks done. What works is +C on the DIRECTORY, which
     files created afterwards inherit.
  2. IT IS BTRFS-ONLY, and on ext4/xfs a bare chattr prints an error during an install for a tuning
     step that simply does not apply.

`os/gentoo.sh` used to be exactly `chattr -R +C $i`, unquoted, on three paths, with no filesystem
check. These tests run the real functions.

The btrfs half needs a loop-mounted image and therefore root; it SKIPS rather than failing where it
cannot run, and the parts that are pure shell logic are checked everywhere.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(ROOT, "os", "gentoo.sh")
PROVISION = os.path.join(ROOT, "os", "bin", "pc-provision-user")


def _have(*bins):
    return all(shutil.which(b) for b in bins)


@unittest.skipIf(not os.path.exists(SH), "no os/gentoo.sh here")
class TheScriptSaysWhatItDoes(unittest.TestCase):
    """Read, not run — these hold whether or not this machine has btrfs or root."""

    def setUp(self):
        self.src = open(SH, encoding="utf-8").read()
        # The comments EXPLAIN the no-op by naming it, so a plain substring search finds the string
        # in the very block that documents why it is wrong. Only the code may contain it.
        self.code = "\n".join(l for l in self.src.split("\n") if not l.lstrip().startswith("#"))

    def test_the_recursive_no_op_is_gone(self):
        """`chattr -R +C` over a populated tree is the whole bug: it succeeds and converts nothing.
        If it comes back, this fails — including the unquoted `$i` it used to carry, which breaks on
        any path with a space in it."""
        self.assertNotIn("chattr -R +C", self.code,
                         "chattr -R +C is back — on existing files it does nothing at all")

    def test_it_checks_the_filesystem_before_it_tries(self):
        """ext4/xfs/zfs have no such attribute, and a scary error in the middle of an install for a
        step that does not apply there is how people start ignoring installer output."""
        self.assertIn("stat -f -c %T", self.code, "nothing checks the filesystem type")
        self.assertRegex(self.code, r'!=\s*"btrfs"', "nothing skips a non-btrfs path")

    def test_there_is_only_one_of_these_functions(self):
        """There were TWO — `btrfsTweaks` and `btrfs-tweaks` — with different lists. `/var/lib/docker`
        and `/volumes` were only in one, `/var/lib/postgresql` only in the other, and only one name
        was reachable from the command line, so half the paths were never touched by anything at
        all. Both were the naive recursive form."""
        bodies = re.findall(r"DISABLE_COW=\(", self.code)
        self.assertEqual(len(bodies), 1,
                         "there is more than one nodatacow path list — they will drift, and one of "
                         "them will be the one nothing calls")
        for path in ("/var/lib/postgresql", "/var/lib/docker", "/volumes"):
            self.assertIn(path, self.code, path + " was dropped when the two lists were merged")

    def test_the_trade_is_written_down(self):
        """nodatacow also turns off checksums and opts these files out of compression. That is the
        intended bargain for a database and it is NOT obvious, so the next person to read this must
        not "fix" it."""
        self.assertRegex(self.src, r"checksum", "the checksum trade is not stated")
        self.assertRegex(self.src, r"compress", "the compression trade is not stated")

    def test_rewriting_existing_data_is_a_separate_deliberate_command(self):
        """The only way to convert a file that already has extents is to write its contents into a
        new one. Doing that under a running database is data loss, so it must not be part of an
        install."""
        self.assertIn("btrfs-tweaks-rewrite", self.code, "there is no way to convert existing data")
        body = self.src[self.src.index("nodatacowRewrite() {"):]
        self.assertIn("read -p", body, "the rewrite does not ask before rewriting every file")
        self.assertIn("--reflink=never", body,
                      "a reflink copy SHARES the old copy-on-write extents, which is the thing "
                      "being undone — the copy has to be a real one")

    def test_the_busiest_path_is_handled_where_it_is_still_empty(self):
        """~/.config/posterchan-desktop holds the client's local relay (IndexedDB). Accounts are
        created at sign-in, long after the installer, so the installer CANNOT set +C on it — and by
        the time anything else could, it is populated and +C no longer takes."""
        prov = open(PROVISION, encoding="utf-8").read()
        self.assertIn("chattr +C", prov, "the per-user profile never gets nodatacow")
        self.assertIn("posterchan-desktop", prov)
        self.assertIn("stat -f -c %T", prov, "the provisioner does not check for btrfs")
        # …and BEFORE anything can write into it. `mkdir` then `chattr` on the directory, with the
        # attribute set before the app has ever run.
        i_mk = prov.index('mkdir -p "$PROFILE_DIR"')
        # The CODE line, not the comment above it that explains why the order matters.
        i_ch = prov.index('chattr +C "$PROFILE_DIR"')
        self.assertLess(i_mk, i_ch, "the directory must exist before +C is set on it")

    def test_both_copies_of_the_provisioner_agree(self):
        """The installer writes one and the overlay package ships the other; a line in only one of
        them works on exactly half the installs."""
        other = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchanos-shell",
                             "files", "pc-provision-user")
        if not os.path.exists(other):
            self.skipTest("no overlay copy here")
        self.assertEqual(open(PROVISION, encoding="utf-8").read(),
                         open(other, encoding="utf-8").read(),
                         "the two copies of pc-provision-user have drifted")


@unittest.skipIf(os.geteuid() != 0 or not _have("mkfs.btrfs", "chattr", "lsattr", "mount"),
                 "needs root and btrfs tools to make a real filesystem")
class OnARealBtrfs(unittest.TestCase):
    """The claims above, against an actual btrfs. This is the only way to show that the OLD spelling
    silently fails — no amount of reading the script proves an ioctl did nothing."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.img = os.path.join(cls.tmp, "fs.img")
        subprocess.run(["truncate", "-s", "300M", cls.img], check=True)
        subprocess.run(["mkfs.btrfs", "-q", cls.img], check=True)
        cls.mnt = os.path.join(cls.tmp, "mnt")
        os.makedirs(cls.mnt)
        subprocess.run(["mount", "-o", "loop", cls.img, cls.mnt], check=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["umount", cls.mnt], check=False)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def attrs(path):
        out = subprocess.run(["lsattr", "-d", path], capture_output=True, text=True)
        return out.stdout.split()[0] if out.stdout else ""

    def test_plus_C_does_nothing_to_a_file_that_already_has_data(self):
        """The gotcha itself, demonstrated. This is why `-R` over a populated tree is a lie."""
        d = os.path.join(self.mnt, "populated")
        os.makedirs(d)
        f = os.path.join(d, "already.db")
        with open(f, "wb") as fh:
            fh.write(b"x" * (1 << 20))
        subprocess.run(["chattr", "-R", "+C", d], capture_output=True)
        self.assertNotIn("C", self.attrs(f),
                         "chattr -R +C converted an existing file — if this ever passes, btrfs "
                         "changed and this whole design can be simplified")

    def test_plus_C_on_the_directory_is_inherited_by_new_files(self):
        """…and the thing that DOES work."""
        d = os.path.join(self.mnt, "fresh")
        os.makedirs(d)
        subprocess.run(["chattr", "+C", d], check=True)
        f = os.path.join(d, "new.db")
        with open(f, "wb") as fh:
            fh.write(b"y" * 4096)
        self.assertIn("C", self.attrs(f), "a file created in a +C directory did not inherit it")

    def test_the_rewrite_converts_what_was_already_there(self):
        """`nodatacowRewrite` copies each file into the +C directory and renames over the original,
        which is the only way to give existing data nodatacow."""
        d = os.path.join(self.mnt, "convert")
        os.makedirs(d)
        f = os.path.join(d, "old.db")
        with open(f, "wb") as fh:
            fh.write(b"z" * (1 << 20))
        # The rewrite's inner loop, LIFTED OUT OF THE SCRIPT rather than sourced. `gentoo.sh` runs
        # its own top-level code on source (it mkdirs, and it has an interactive menu that recurses
        # on empty input) — sourcing it here segfaulted bash. So the line under test is extracted by
        # name from the real file, which keeps this honest without booting an installer.
        body = open(SH, encoding="utf-8").read()
        body = body[body.index("nodatacowRewrite() {"):]
        self.assertIn("--reflink=never", body)
        r = subprocess.run(["bash", "-c",
                            'chattr +C "$1"; '
                            'cp --preserve=all --reflink=never "$2" "$2.n" && mv -f "$2.n" "$2"',
                            "_", d, f], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        self.assertIn("C", self.attrs(f), "the rewritten file is still copy-on-write")
        with open(f, "rb") as fh:
            self.assertEqual(len(fh.read()), 1 << 20, "the rewrite changed the contents")


if __name__ == "__main__":
    unittest.main()
