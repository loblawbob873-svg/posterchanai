"""AN INSTALL THAT THE FIRMWARE IS NEVER TOLD ABOUT IS NOT A BOOTABLE SYSTEM.

Measured 2026-09-17 on a real machine that had been running Windows. The installer completed: correct
GPT, a 2 GB ESP, a 475 GB LUKS volume, btrfs subvolumes, `EFI/systemd/systemd-bootx64.efi`,
`EFI/BOOT/BOOTX64.EFI`, a loader entry naming the right kernel, initrd, `root=UUID=`, `rd.luks.uuid=`
and a keyfile that genuinely opens the volume. It still would not boot, because the firmware had never
heard of it:

    BootOrder: 0000,000B,0009,0013,000F,0012,000E,000D,000A,0003,0007,...
    Boot0000* Windows Boot Manager      <- first, and Windows had just been erased
    Boot0001* Linux Boot Manager        <- buried
    (no PosterChanOS entry at all)

The cause is one flag, `os/gentoo.sh`'s `bootctl --esp-path=/boot --no-variables install`:
`--no-variables` writes the loader to the ESP and deliberately does NOT create the EFI boot variable.
Creating it by hand (`efibootmgr -c -d /dev/sda -p 1 -l '\\EFI\\systemd\\systemd-bootx64.efi'`) made
the machine boot that instant.

WHY EVERY VM TEST PASSED, AND WHY THE CODE SAYS SO OUT LOUD. The comment above that call reads "The
fallback loader is what fresh VM NVRAM boots first" — which is exactly right and exactly the blind
spot. A guest with fresh OVMF variables has no boot entries, so the firmware falls back to the
removable-media path `EFI/BOOT/BOOTX64.EFI` and boots. A machine that has ever run another OS has a
populated NVRAM whose stale entries are tried first, and the fallback is never reached. So
`check_livecd_install_vm.py` can install and boot successfully on a disk that is unbootable on real
hardware, which is what happened: the gate is honest about the artifact it boots and blind to the one
fact that differs.

This file therefore pins the RULE (the installer registers itself with the firmware and says so) at
the level a unit test can reach, since NVRAM cannot be exercised here.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENTOO = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")


def _call_site() -> str:
    """The region around the efibootmgr CALL.

    Anchored on `efibootmgr -c` because `efibootmgr` also appears in BASE_PACKAGES; slicing from the
    first mention read the package list and failed about flags that were present.
    """
    i = GENTOO.index("efibootmgr -c")
    start = GENTOO.rfind("if [ -d /sys/firmware/efi ]", 0, i)
    return GENTOO[(start if start != -1 else max(0, i - 3000)):i + 2000]


class InstallerRegistersABootEntry(unittest.TestCase):
    def test_something_creates_an_efi_boot_variable(self):
        """Either bootctl is allowed to write variables, or efibootmgr is called. Not neither."""
        writes_vars = re.search(r"bootctl[^\n]*install", GENTOO) and not re.search(
            r"bootctl[^\n]*--no-variables[^\n]*install", GENTOO)
        # The CALL, not the mention: `efibootmgr` is also in BASE_PACKAGES, and matching that would
        # let this pass on an installer that merely ships the tool and never runs it.
        calls_efibootmgr = "efibootmgr -c" in GENTOO
        self.assertTrue(
            writes_vars or calls_efibootmgr,
            "the installer writes a bootloader to the ESP and never tells the firmware about it; on "
            "a machine with existing NVRAM entries (any ex-Windows machine) it boots the old ones for "
            "ever. See this file's docstring for the measurement.")

    def test_the_entry_names_the_esp_partition_and_the_loader(self):
        """A boot entry needs the disk, the partition NUMBER and the loader path inside the ESP."""
        if "efibootmgr -c" not in GENTOO:
            self.skipTest("bootctl is writing variables itself")
        block = _call_site()
        self.assertRegex(block, r"-d\s", "no -d <disk> — efibootmgr would guess the disk")
        self.assertRegex(block, r"-p\s", "no -p <partnum> — efibootmgr would guess the partition")
        self.assertRegex(block, r"systemd-bootx64\.efi",
                         "the entry does not name the loader that was installed")

    def test_a_reinstall_does_not_pile_up_duplicate_entries(self):
        """NVRAM is small and finite; the machine measured already held 17 entries, most of them dead."""
        if "efibootmgr -c" not in GENTOO:
            self.skipTest("bootctl is writing variables itself")
        block = _call_site()
        self.assertRegex(
            block, r"-B|--delete-bootnum|delete",
            "nothing removes a previous PosterChanOS entry, so every reinstall adds another")

    def test_it_is_never_silent_about_failing(self):
        """efivarfs is not always writable (a chroot without /sys, a BIOS boot, a locked-down board).

        Failing quietly there reproduces exactly the bug this file exists for, so the refusal has to
        name the manual command instead of leaving a machine that installs and cannot boot.
        """
        if "efibootmgr -c" not in GENTOO:
            self.skipTest("bootctl is writing variables itself")
        block = _call_site()
        self.assertTrue(
            re.search(r"(echo|printf)[^\n]*(firmware|boot entry|efibootmgr)", block, re.I),
            "an efibootmgr failure prints nothing — the install would look complete and not boot")

    def test_efibootmgr_is_a_shipped_package(self):
        """It has to be ON the live image, since that is where the installer runs."""
        pkgs = re.search(r"(?m)^POSTERCHANOS_PACKAGES=\"(.*?)\"", GENTOO, re.S)
        base = re.search(r"(?m)^BASE_PACKAGES=\"(.*?)\"", GENTOO, re.S)
        have = (pkgs.group(1) if pkgs else "") + " " + (base.group(1) if base else "")
        if "efibootmgr" not in GENTOO:
            self.skipTest("bootctl is writing variables itself")
        self.assertIn(
            "efibootmgr", have,
            "the installer calls efibootmgr but nothing guarantees it is in the image; on a build "
            "where it arrives only as somebody else's dependency this breaks with 'command not found'")


if __name__ == "__main__":
    unittest.main()
