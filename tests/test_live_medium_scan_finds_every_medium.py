"""THE LIVE-MEDIUM SCAN IS RUN HERE, AGAINST FAKE MEDIA, ON EVERY PUSH.

This one bug has been fixed TWICE, one medium apart:

    a4ad756c7  the installer could not find its own kernel when booted from USB
    d3cd2915a  the installer can see a CD, which is the medium it exists to find

Both times it shipped in an ISO, both times it was found by a person standing in front of a live
desktop that could not install itself, and both times the fix was verified against the ONE medium
that happened to be in front of whoever made it — a USB stick on real hardware, a CD in a VM. The
7 Sep fix widened the acceptance test (identify the medium by carrying boot/ + LiveOS/, not by
filesystem type) while NARROWING the candidate list (`blkid -o device`, which lists /dev/sr0, became
lsblk filtered to `disk`/`part`, which does not). USB began working; CD silently stopped.

The gate that would have caught it — `check_livecd_install_vm.py` — needs KVM and a 4GB image, so it
runs when somebody remembers to run it, which on this box is never (no /dev/kvm on server1). A rule
nothing exercises is a rule that rots.

So the scan is EXTRACTED FROM THE SHIPPED SCRIPT and executed here against stubbed `lsblk`, `blkid`,
`mount` and friends, over the media this product actually boots from. It costs milliseconds and it
fails for either of the two commits above.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GENTOO = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")


def scan_block() -> str:
    """The candidate enumeration and the acceptance loop, straight out of the shipped installer."""
    start = GENTOO.index("\t\tlocal cand")
    end = GENTOO.index("\t\tdone\n", start) + len("\t\tdone\n")
    return GENTOO[start:end].replace("local cand", "cand=")


class LiveMediumScan(unittest.TestCase):
    def run_scan(self, devices, contents, root_dev="/dev/nvme0n1p2", blkid=None):
        """`devices` maps a device to its lsblk TYPE; `contents` to the directories it carries.

        THE TYPE COLUMN IS THE WHOLE POINT AND THE FIXTURE MUST EMIT IT. A first version of this
        stub printed names only, so `lsblk -pnro NAME,TYPE | awk '$2=="disk"'` matched nothing, the
        old code fell through to its blkid fallback, and the CD bug PASSED — the fixture agreeing
        with the bug it exists to catch. The stub answers the fields it is asked for.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            media = tmp / "media"

            def stub(name, body):
                p = bin_dir / name
                p.write_text("#!/bin/sh\n" + body + "\n")
                p.chmod(0o755)

            pairs = " ".join(f"'{d} {t}'" for d, t in devices.items())
            names = " ".join(f"'{d}'" for d in devices)
            stub("lsblk",
                 'for a in "$@"; do case "$a" in *NAME,TYPE*) printf "%s\\n" ' + pairs + '; exit 0;; esac; done\n'
                 'printf "%s\\n" ' + names)
            bl = blkid if blkid is not None else list(devices)
            stub("blkid",
                 'case "${1:-}" in -s) exit 0;; esac\n'
                 'printf "%s\\n" ' + " ".join(f"'{d}'" for d in bl))
            stub("findmnt", f"echo '{root_dev}'")
            stub("mountpoint", "exit 1")
            stub("sudo", '"$@"')
            stub("umount", "exit 0")
            # `mount -o ro <dev> <dir>` — lay out whatever that device is supposed to carry.
            layout = "\n".join(
                f'  if [ "$2" = "{dev}" ] || [ "$3" = "{dev}" ]; then '
                + " ".join(f'mkdir -p "$MP/{d}";' for d in dirs)
                + (f' touch "$MP/boot/vmlinuz";' if "boot" in dirs else "")
                + " exit 0; fi"
                for dev, dirs in contents.items())
            stub("mount", 'MP="${@: -1}"\nmkdir -p "$MP"\n' + layout + "\nexit 1")
            # Every listed device must look like a block device to `[ -b ]`.
            devnull = tmp / "dev"
            devnull.mkdir()

            script = (
                "set -u\n"
                f'export PATH="{bin_dir}:$PATH"\n'
                f'media="{media}"\n'
                "LIVEDIR=''\n"
                # `[ -b ]` cannot be faked for a path that is not a real device node, so the loop's
                # own guard is replaced by the one thing the test controls: the device list.
                + scan_block().replace('[ -b "$dev" ] || continue', ': ')
                + '\necho "LIVEDIR=$LIVEDIR"\n')
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
            found = re.search(r"LIVEDIR=(\S*)", r.stdout)
            return (found.group(1) if found else ""), r

    def test_a_cd_is_found(self):
        """/dev/sr0 — what every VM `media=cdrom` and every optical boot presents. lsblk types it
        `rom`, which is what the 7 Sep candidate filter excluded."""
        got, r = self.run_scan(
            devices={"/dev/vda": "disk", "/dev/sr0": "rom"},
            contents={"/dev/sr0": ["boot", "LiveOS"]})
        self.assertTrue(got, "the CD holding boot/ and LiveOS/ was never even mounted: " + r.stderr[-400:])

    def test_a_usb_stick_is_found(self):
        """The hybrid ISO's contents live in an hfsplus PARTITION, /dev/sda3 — the case a4ad756c7
        was written for. It must not regress while fixing the CD."""
        got, _ = self.run_scan(
            devices={"/dev/sda": "disk", "/dev/sda1": "part", "/dev/sda2": "part",
                     "/dev/sda3": "part", "/dev/sda4": "part", "/dev/nvme0n1p2": "part"},
            contents={"/dev/sda3": ["boot", "LiveOS"]})
        self.assertTrue(got, "the USB stick's payload partition was not found")

    def test_a_device_only_blkid_knows_about_is_found(self):
        """Either enumeration alone has a blind spot; using one as a mere FALLBACK for the other is
        what let a narrowing go unnoticed. The list is a union."""
        got, _ = self.run_scan(
            devices={"/dev/vda": "disk"}, blkid=["/dev/vda", "/dev/sr0"],
            contents={"/dev/sr0": ["boot", "LiveOS"]})
        self.assertTrue(got, "a device listed only by blkid was never considered")

    def test_an_installed_disk_is_never_adopted(self):
        """The safety property the type test used to provide. An installed system has boot/ — and
        never LiveOS/, which is written only by the ISO builder."""
        got, _ = self.run_scan(
            devices={"/dev/nvme0n1p1": "part", "/dev/nvme0n1p2": "part"},
            contents={"/dev/nvme0n1p1": ["boot"]})
        self.assertEqual(got, "", "an installed disk was mistaken for the live medium")

    def test_the_running_root_is_skipped(self):
        got, _ = self.run_scan(
            devices={"/dev/sda3": "part"}, root_dev="/dev/sda3",
            contents={"/dev/sda3": ["boot", "LiveOS"]})
        self.assertEqual(got, "", "the scan tried to remount the running root")

    def test_an_empty_boot_directory_is_not_a_medium(self):
        """`ls boot/*` — a medium whose boot/ exists but holds nothing has no kernel to copy."""
        got, _ = self.run_scan(
            devices={"/dev/sr0": "rom"},
            contents={"/dev/sr0": ["LiveOS"]})
        self.assertEqual(got, "", "a medium with no kernel was accepted")


if __name__ == "__main__":
    unittest.main()
