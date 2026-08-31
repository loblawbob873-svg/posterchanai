"""The only gate that boots a real PosterChanOS must be able to run without an argument.

`check_livecd_vm.py` boots a LiveCD ISO — or a disk the installer produced, with no media attached —
and proves a graphical session appears and stays. It is the only thing in this repository that can
catch "the new installed system does not boot, no EFI entries" before somebody's machine does.

It SKIPPED in every suite run ever recorded, with `error: one of the arguments iso --disk is
required`: checkall invokes a check with no arguments, and nothing supplied one. A gate that cannot
run is a file, and installer regressions therefore reached a person instead of a build — which is
exactly how they were reported, repeatedly.

So the target comes from the environment when the command line does not carry it, and an absent
image is a SKIP (exit 2), never a pass. Verified for real on hardware with KVM: the live ISO and an
installed disk both boot to a stable desktop, driven by these two variables alone.
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "check_livecd_vm.py"


class TheGateRunsWithoutArguments(unittest.TestCase):
    def test_no_image_configured_is_a_skip_not_a_pass(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("PC_LIVECD_ISO", "PC_INSTALLED_DISK")}
        out = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True,
                             timeout=120, env=env)
        self.assertEqual(2, out.returncode,
                         "with no image configured this must exit 2 (SKIP). Exit 0 would report a "
                         "boot nobody tested; exit 1 would make every machine without an ISO red.")
        self.assertIn("not a pass", out.stdout,
                      "the skip must say plainly that nothing was verified")

    def test_a_missing_file_is_also_a_skip(self):
        env = dict(os.environ, PC_LIVECD_ISO="/definitely/not/here.iso")
        env.pop("PC_INSTALLED_DISK", None)
        out = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True,
                             timeout=120, env=env)
        self.assertEqual(2, out.returncode, out.stdout + out.stderr)

    def test_both_targets_are_documented_in_the_source(self):
        """Two separate questions: the image boots, and what the INSTALLER PRODUCED boots. The
        second is the one that catches a missing bootloader, and it needs no installer media."""
        src = CHECK.read_text(encoding="utf-8")
        self.assertIn("PC_LIVECD_ISO", src)
        self.assertIn("PC_INSTALLED_DISK", src)
        self.assertIn("--disk", src, "the installed-disk mode is what proves EFI actually works")


if __name__ == "__main__":
    unittest.main()
