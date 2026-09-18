"""AN ENCRYPTED INSTALL MUST BOOT A KERNEL THAT HAS dm-crypt, AND ONE SHIPPED THAT DID NOT.

Measured 2026-09-18 on the machine this was reported from. The install completed, the EFI boot entry
was correct, the firmware started it, the keyfile opened the volume by hand, `/etc/crypttab` and the
kernel command line named the right UUIDs, and the initramfs carried crypttab, the keyfile and
systemd-cryptsetup. It still could not boot:

    device-mapper: unknown target type: crypt
    Failed to start Cryptography Setup for luks-<uuid>

Because `CONFIG_DM_CRYPT=m` and the kernel it booted — 6.18.48-gentoo-dist-bin — had an INCOMPLETE
module tree on the build host with no `dm-crypt.ko` in it, while 6.18.43 had one. The initramfs
carries no modules at all (everything else it needs is built in), so device-mapper had no `crypt`
target to create. Every artefact a person would check looked perfect; the missing thing was a file
nobody thought to look for.

TWO FAULTS, AND THE SECOND MADE THE FIRST UNSURVIVABLE:

  1. `bootloader()` took the NEWEST kernel (`ls /usr/lib/modules | sort -V | tail -1`, and the newest
     directory under /boot/<machine-id>), which is not the same as one that works.
  2. The module cleanup then runs `ls /usr/lib/modules | grep -Evi "$KERNEL_VERSION" | xargs -r rm -r`
     — deleting every other tree, so the WORKING 6.18.43 modules were removed from the disk on the
     way past and the machine had no usable kernel left at all.

AND WHY THE VM GATE SAID YES: `check_livecd_install_vm.py` accepted `Reached target` as proof of a
boot. An emergency shell reaches targets too. The guest failed the same way and was reported as a
pass, which is how this reached a user.
"""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENTOO = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    start = GENTOO.index(f"{name}() {{")
    return GENTOO[start:GENTOO.index("\n}\n", start) + 3]


class KernelChoice(unittest.TestCase):
    def run_chooser(self, trees):
        """RUN the shipped chooser against a fake /usr/lib/modules.

        `trees` maps a kernel version to whether its tree contains dm-crypt.ko.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mods = tmp / "usr/lib/modules"
            for ver, has in trees.items():
                d = mods / ver / "kernel/drivers/md"
                d.mkdir(parents=True)
                (mods / ver / "modules.builtin").write_text("")
                if has:
                    (d / "dm-crypt.ko").write_text("")
            script = (
                "set -u\n"
                # The shipped functions read absolute paths; point them at the fake tree.
                + (_fn("pc_kernel_can_unlock") + _fn("pc_best_kernel_version"))
                    .replace("/usr/lib/modules", str(mods))
                + '\necho "PICKED=$(pc_best_kernel_version || echo NONE)"\n')
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
            m = re.search(r"PICKED=(\S*)", r.stdout)
            self.assertIsNotNone(m, r.stdout + r.stderr)
            return m.group(1)

    def test_it_refuses_the_newest_kernel_when_that_one_cannot_unlock(self):
        """THE REPORTED MACHINE, exactly: 6.18.48 is newer and broken, 6.18.43 works."""
        picked = self.run_chooser({"6.18.43-gentoo-dist-bin": True,
                                   "6.18.48-gentoo-dist-bin": False})
        self.assertEqual("6.18.43-gentoo-dist-bin", picked,
                         "it chose a kernel with no dm-crypt module — the installed system cannot "
                         "open its own disk and says 'unknown target type: crypt'")

    def test_it_still_prefers_the_newest_usable_one(self):
        picked = self.run_chooser({"6.18.43-gentoo-dist-bin": True,
                                   "6.18.48-gentoo-dist-bin": True})
        self.assertEqual("6.18.48-gentoo-dist-bin", picked,
                         "a working newer kernel must still win; this is not a downgrade rule")

    def test_built_in_dm_crypt_counts(self):
        """Some kernels build it in; absence of a .ko is then not absence of the target."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            mods = tmp / "usr/lib/modules/6.19.0-x"
            mods.mkdir(parents=True)
            (mods / "modules.builtin").write_text("kernel/drivers/md/dm-crypt.ko\n")
            script = ("set -u\n"
                      + (_fn("pc_kernel_can_unlock") + _fn("pc_best_kernel_version"))
                        .replace("/usr/lib/modules", str(tmp / "usr/lib/modules"))
                      + '\necho "PICKED=$(pc_best_kernel_version || echo NONE)"\n')
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
            self.assertIn("PICKED=6.19.0-x", r.stdout, r.stdout + r.stderr)

    def test_nothing_usable_is_a_refusal_not_a_silent_install(self):
        picked = self.run_chooser({"6.18.48-gentoo-dist-bin": False})
        self.assertEqual("NONE", picked)
        self.assertIn("No installed kernel has a dm-crypt module", GENTOO,
                      "an install with no usable kernel completes silently and cannot boot")

    def test_the_boot_entry_path_is_guarded_too(self):
        """The failing machine got its version from /boot/<machine-id>, not from the fallback."""
        block = GENTOO[GENTOO.index('KERNEL_VERSION="$(find "/boot/$MACHINE_ID"'):]
        block = block[:block.index("LOADER_FILE=")]
        self.assertIn("pc_kernel_can_unlock", block,
                      "the path that actually chose the broken kernel is unguarded")


class TheVmGateMustNotCallEmergencyModeABoot(unittest.TestCase):
    """`Reached target` is printed on the way into an emergency shell as well as a desktop."""

    def test_reached_target_alone_is_not_proof_of_a_boot(self):
        src = (ROOT / "scripts/check_livecd_install_vm.py").read_text(encoding="utf-8")
        good = re.search(r'good = r"\(([^"]+)\)"', src)
        self.assertIsNotNone(good, "the boot check's success pattern moved")
        self.assertNotIn("Reached target", good.group(1),
                         "a bare 'Reached target' accepts an emergency shell as a successful boot — "
                         "that is how an unbootable image was reported as passing")


if __name__ == "__main__":
    unittest.main()
