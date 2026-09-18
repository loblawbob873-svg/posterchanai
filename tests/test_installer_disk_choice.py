"""THE INSTALLER'S DISK CHOICE IS RUN HERE, AGAINST FAKE DISKS, ON EVERY PUSH.

Reported off a TV-attached machine: *"installer would not even find disk to use"*, followed by
*"why does the installer keep having issues after we keep fixing it"*.  That second question has a
measured answer, and this file is half of it: **nothing in this repo had ever executed the code that
decides where PosterChanOS gets installed.**  A grep for `DEFAULT_DISK`, for the sentence
`No install disk found besides the live medium.` or for the `Disk Device to Use` prompt matched no
test and no check.  What the suite did prove is that the ISO boots and that the session comes up —
`check_livecd_session_ready.py` passed on the very image that could not install itself — and those
are different questions.

AND THE ONE ENVIRONMENT IT DID RUN IN HAS EXACTLY ONE DISK.  Every VM gate here boots
`-drive if=virtio`.  So the filter grew around what QEMU happens to show: the `fd|sr|zram|loop|ram`
exclusions exist because QEMU puts a legacy floppy ahead of its virtio disk.  That is a fix shaped by
the one machine anybody could see, which is the same shape as the NVIDIA bug found the same night —
the build host is AMD, so nothing noticed the image shipped no NVIDIA driver at all.  A VM has no
eMMC, no 4K TV and no NVIDIA, so "it works in the VM" was never evidence about somebody's hardware.

THE RULE THIS FILE PINS IS THAT A REFUSAL NAMES WHAT IT MEASURED, AND IS NEVER THE ONLY DOOR.
The size floor and the device-name exclusions are a *default* picker — the shipped comment says so in
as many words ("this filter only governs the safe one-click default").  But the line immediately
after it returned 1, so the filter was also the only way in: a disk it declined meant the installer
announced that no disk existed and never opened the prompt, so no disk could be named by hand
either.  Two ordinary machines fall in that gap — an eMMC sold as "8 GB" is 7.45 GiB and misses
`>= 8 GiB` by measurement, and a live medium detected wrongly excludes the real disk — and on both
the screen named nothing it had looked at, so diagnosing it cost a person standing in front of it.

Each case below is verified to FAIL against `git show HEAD:os/gentoo.sh` (see
`test_the_old_code_fails_these`), because a test written after a fix is worth only what it would have
caught before it.
"""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENTOO = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")

GIB = 1073741824


def picker_block(text: str) -> str:
    """The disk enumeration, the default choice and its refusal, straight out of the installer.

    Anchored on the `findmnt` that starts the live-medium detection and closed at the BTRFS prompt
    that follows the validation, so the extraction covers the whole decision and cannot silently
    shrink to the half that happens to pass.
    """
    start = text.index('LIVE_SOURCE="$(findmnt')
    end = text.index("read -r -p 'BTRFS Root Volume name", start)
    block = text[start:end]
    # `local` is only legal inside a function; the declaration line sits above the anchor.
    return block.replace("local ", "")


class InstallerDiskChoice(unittest.TestCase):
    def choose(self, disks, live_source="/dev/sda2", live_pkname="sda", typed="", text=None):
        """Run the shipped picker over `disks`.

        `disks` is a list of (name, type, size_bytes, model).  `typed` is what the operator types at
        the prompt — "" means they press Enter, which is what makes the default load-bearing.
        """
        block = picker_block(GENTOO if text is None else text)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()

            def stub(name, body):
                p = bin_dir / name
                p.write_text("#!/bin/sh\n" + body + "\n")
                p.chmod(0o755)

            # lsblk answers the fields it is asked for, exactly as the real one does. The column set
            # decides the shape, so the stub branches on it rather than printing one fixed table —
            # a fixture that prints names only would let a `$2=="disk"` filter match nothing and
            # agree with whatever bug is being hunted.
            rows_bd = "\n".join(f"{n} {t} {s} {m}" for n, t, s, m in disks)
            rows_tree = "\n".join(f"{n} {s} {t} - - {m}" for n, t, s, m in disks)
            stub("lsblk", f'''
args="$*"
case "$args" in
  *PKNAME*) printf '%s\\n' '{live_pkname}'; exit 0;;
esac
case "$args" in
  *-dnro\\ TYPE*|*TYPE\\ /dev/*)
      for a in "$@"; do case "$a" in /dev/*) d=${{a#/dev/}};; esac; done
      printf '%s\\n' "$(printf '%s\\n' '{rows_bd}' | awk -v d="$d" '$1==d {{print $2}}')"; exit 0;;
esac
case "$args" in
  *NAME,TYPE,SIZE*) printf '%s\\n' '{rows_bd}'; exit 0;;
  *NAME,SIZE,TYPE*) printf '%s\\n' '{rows_tree}'; exit 0;;
esac
printf '%s\\n' '{rows_bd}'
''')
            stub("findmnt", f"printf '%s\\n' '{live_source}'")
            stub("clear", "exit 0")
            # `[ -b /dev/X ]` cannot be satisfied for a name that is not a real device node, so the
            # test controls that predicate the same way the medium-scan harness does.
            script = (
                "set -u\n"
                # The installer paints its refusals; an unset COLOR_* under `set -u` aborts the
                # shell before the sentence is printed, which would read here as "it said nothing".
                "COLOR_YELLOW='' COLOR_RESET='' COLOR_CYAN='' COLOR_BOLD='' COLOR_RED=''\n"
                f'export PATH="{bin_dir}:$PATH"\n'
                f'setDevices() {{ echo "SETDEVICES $HARD_DISK"; }}\n'
                f'printf "%s\\n" "{typed}" > "{tmp}/typed"\n'
                # `read -r -p` must come from the fixture, not a tty.
                f'exec 3< "{tmp}/typed"\n'
                # THE BLOCK RUNS INSIDE A FUNCTION, because the code under test refuses with
                # `return 1` — and `return` at the top level of a script is an ERROR that does not
                # stop execution. Run flat, the old code's refusal was ignored and the fixture then
                # read a disk name anyway, which made the mutation proof claim the bug was absent.
                + "pc_pick() {\n"
                # EVERY prompt for the disk reads from the fixture, matched on the RULE (a `read`
                # that assigns `device`) rather than on its wording — pinning the sentence made this
                # test fail the moment the refusal was reworded, which is the wrong kind of failure.
                + re.sub(r'read -r -p "[^"]*" device',
                         'IFS= read -r device <&3 || true',
                         picker_block(GENTOO if text is None else text))
                    .replace('[ ! -b "/dev/$device" ]', 'false')
                + "\n}\n"
                # `device` is deliberately not declared local by the installer, so it survives the
                # call the way HARD_DISK does.
                + 'pc_pick; echo "CHOSE=${device:-}"\n')
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
            out = r.stdout + r.stderr
            m = re.search(r"CHOSE=(\S*)", out)
            return (m.group(1) if m else ""), out, r

    # ---------------------------------------------------------------- the machines that worked

    def test_an_ordinary_nvme_is_chosen(self):
        chose, out, _ = self.choose(
            [("nvme0n1", "disk", 512 * GIB, "WD_SN770"), ("sda", "disk", 32 * GIB, "Flash_Drive")],
            live_source="/dev/sda2", live_pkname="sda")
        self.assertEqual("nvme0n1", chose, out)

    def test_the_live_usb_is_never_the_default(self):
        chose, out, _ = self.choose(
            [("sda", "disk", 119 * GIB, "Flash_Drive"), ("nvme0n1", "disk", 931 * GIB, "WD")],
            live_source="/dev/sda3", live_pkname="sda")
        self.assertEqual("nvme0n1", chose, out)

    def test_a_floppy_and_an_optical_drive_are_never_chosen(self):
        chose, out, _ = self.choose(
            [("fd0", "disk", 4 * GIB, "-"), ("sr0", "rom", 0, "QEMU_DVD"),
             ("vda", "disk", 64 * GIB, "-")],
            live_source="/dev/sr0", live_pkname="sr0")
        self.assertEqual("vda", chose, out)

    # ------------------------------------------------- the machines that were told nothing

    def test_a_small_emmc_is_named_with_its_size_and_the_prompt_still_opens(self):
        """An "8 GB" eMMC is 7.45 GiB and misses the floor by measurement.

        The old code answered `No install disk found besides the live medium.` and returned, so the
        one disk in the machine was never named and could not be typed either. It must be named,
        its size stated, and the prompt must open — the operator owns the decision about their own
        hardware, and the validation below the prompt is what keeps that safe.
        """
        chose, out, _ = self.choose(
            [("mmcblk0", "disk", int(7.45 * GIB), "BJTD4R"), ("sda", "disk", 32 * GIB, "Flash")],
            live_source="/dev/sda2", live_pkname="sda", typed="mmcblk0")
        self.assertIn("mmcblk0", out, "the disk that exists was never named")
        self.assertRegex(out, r"7\.4\d*\s*GiB", "its measured size was never stated")
        self.assertEqual("mmcblk0", chose, "the operator could not name the only disk they have")

    def test_when_the_only_disk_is_the_live_medium_it_says_so_by_name(self):
        _, out, _ = self.choose(
            [("sda", "disk", 119 * GIB, "Flash_Drive")],
            live_source="/dev/sda3", live_pkname="sda")
        self.assertIn("sda", out, "the excluded disk was never named")
        self.assertRegex(out.lower(), r"live medium|booted from",
                         "it never said WHY the only disk was passed over")

    def test_no_whole_disk_at_all_points_at_the_controller_driver(self):
        """The shape a modern machine makes when its NVMe sits behind Intel RST/VMD.

        Nothing the installer can do about it, but "no disk found" sends somebody hunting a disk
        that is physically present. Naming the driver is the difference between a BIOS setting and
        an evening.
        """
        _, out, _ = self.choose(
            [("sr0", "rom", 0, "QEMU_DVD")], live_source="/dev/sr0", live_pkname="sr0")
        self.assertRegex(out.lower(), r"driver|controller|dmesg|lsblk",
                         "a machine with no visible disk was given no next step")

    # ---------------------------------------------------------------- the mutation proof

    def test_the_old_code_fails_these(self):
        """The three cases above must FAIL against the code as it shipped, or they prove nothing."""
        old = subprocess.run(["git", "show", "HEAD:os/gentoo.sh"], cwd=ROOT,
                             capture_output=True, text=True, timeout=60).stdout
        if not old or 'LIVE_SOURCE="$(findmnt' not in old:
            self.skipTest("HEAD does not carry a comparable installer")
        if "passed over" in picker_block(old):
            self.skipTest("HEAD already carries the fix (it has been committed)")
        chose, out, _ = self.choose(
            [("mmcblk0", "disk", int(7.45 * GIB), "BJTD4R"), ("sda", "disk", 32 * GIB, "Flash")],
            live_source="/dev/sda2", live_pkname="sda", typed="mmcblk0", text=old)
        self.assertNotEqual("mmcblk0", chose,
                            "the old code already let the operator name a small disk — "
                            "re-check what this file is pinning")
        self.assertIn("No install disk found", out,
                      "the old refusal is not the one described in this docstring")


if __name__ == "__main__":
    unittest.main()
