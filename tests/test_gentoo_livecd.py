"""Turning the running OS into a LiveCD: the rules that decide whether the ISO boots, and whether it
fits on a stick.

`os/gentoo.sh` cannot be run here — it needs root, a Gentoo box, and it reads every file on the disk.
So this reads the function the way a shell would and asserts the handful of things that are silent
when they are wrong:

  * the WORK DIRECTORY excludes itself. Squashing `/` while writing the squashfs into `/` fills the
    disk, and it is the first thing anyone gets wrong.
  * the SWAPFILE is left out, and it is FOUND rather than assumed — a swapfile is usually /swapfile
    and on this installer it is whatever hibernation() made.
  * the swap line is gone from the image's fstab. That is a separate failure from the file: the live
    system would try to swapon something that is not there, which on systemd is a failed unit and a
    red line at every boot.
  * the initramfs is built --no-hostonly. The installed one carries only this machine's drivers and
    its LUKS unlock; booted on somebody else's laptop it finds no root and drops to a shell.
  * the ISO label and the kernel command line agree. A mismatch boots to a dracut shell with nothing
    to say why.
"""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(ROOT, "os", "gentoo.sh")


def _fn(name):
    """One shell function's body, by brace depth — the file is 1700 lines of them."""
    src = open(SH, encoding="utf-8").read()
    i = src.index("\n%s() {" % name)
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("%s never closes" % name)


def _code(body):
    """Without the comments — every rule below is explained in one, naming the thing it forbids."""
    return re.sub(r"(?m)^\s*#.*$", "", body)


@unittest.skipIf(not os.path.exists(SH), "no os/gentoo.sh here")
class LiveCD(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.body = _fn("liveCD")
        cls.code = _code(cls.body)

    def test_the_script_still_parses(self):
        """A syntax error in an installer is discovered by the person running it, on the machine
        they were about to install."""
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("no bash")
        r = subprocess.run([bash, "-n", SH], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_work_directory_excludes_itself(self):
        """Squashing `/` while writing the squashfs into `/` is a loop that fills the disk."""
        self.assertIn('EXCLUDES+=("${OUTDIR#/}")', self.code)

    def test_every_swapfile_is_left_out_and_is_found_rather_than_assumed(self):
        """It is usually /swapfile and on this installer it is whatever hibernation() made — so the
        list comes from what the kernel says is in use AND from fstab, because a swapfile that is
        configured but currently off is still gigabytes of nothing in the image."""
        self.assertIn("/proc/swaps", self.code)
        self.assertIn('$3=="swap"', self.code)
        self.assertIn("SWAPFILES", self.code)
        # And it really reaches the exclude list.
        self.assertRegex(self.code, r'for f in \$SWAPFILES;[\s\S]{0,200}EXCLUDES\+=')

    def test_the_swap_entry_is_gone_from_the_images_fstab(self):
        """A separate failure from the file itself: swapon on a file that is not there is a failed
        unit on every boot of a machine whose whole job is to boot cleanly for a stranger."""
        self.assertIn("fstab.live", self.code)
        # The generated fstab is written from a quoted heredoc, so read it out of the body and check
        # it carries no swap line at all.
        m = re.search(r"<<'FSTAB'\n(.*?)\nFSTAB", self.body, re.S)
        self.assertIsNotNone(m, "the live fstab heredoc moved")
        for line in m.group(1).splitlines():
            if line.strip().startswith("#") or not line.strip():
                continue
            self.assertNotIn("swap", line.lower(), "the live fstab still has a swap entry")

    def test_the_real_fstab_is_never_written_to(self):
        """The image's fstab is built with mksquashfs's pseudo-file feature precisely so that
        nothing on the running system is touched to do it."""
        self.assertIn("-pf ", self.code)
        self.assertIn("etc/fstab f 644", self.code)
        self.assertNotRegex(self.code, r">\s*/etc/fstab")
        self.assertNotRegex(self.code, r"sed -i[^\n]*/etc/fstab")

    def test_the_initramfs_is_not_host_only(self):
        """The installed one carries only this machine's drivers plus its LUKS unlock; booted on
        somebody else's laptop it finds no root and drops to an emergency shell."""
        self.assertIn("--no-hostonly", self.code)
        self.assertIn("dmsquash-live", self.code)
        self.assertIn("--kver", self.code)

    def test_the_iso_label_and_the_kernel_command_line_agree(self):
        """`root=live:CDLABEL=…` is how dmsquash-live finds the medium. A mismatch boots to a dracut
        shell with no clue as to why."""
        self.assertIn("-volid \"$LABEL\"", self.code)
        self.assertIn("root=live:CDLABEL=$LABEL", self.code)
        # …and the three paths on the command line match where the squashfs is actually written.
        self.assertIn("iso/LiveOS/squashfs.img", self.code)
        self.assertIn("rd.live.dir=LiveOS", self.code)
        self.assertIn("rd.live.squashimg=squashfs.img", self.code)

    def test_it_installs_what_it_needs_including_the_uefi_half(self):
        """A missing mtools is the classic one: grub-mkrescue then produces an ISO that boots on BIOS
        and is invisible to every UEFI machine, with only a warning to say so."""
        for pkg in ("squashfs-tools", "libisoburn", "mtools", "dosfstools", "dracut", "grub"):
            self.assertIn(pkg, self.code, "it does not install " + pkg)

    def test_a_failure_stops_rather_than_writing_half_an_iso(self):
        """Every expensive step is checked and stops the build rather than carrying on with a hole
        in the image. The grub step is teed to the transcript now, so its status is read from
        PIPESTATUS rather than from the pipeline — see TheFailureCanActuallyBeREAD."""
        # Anchored on the INVOCATION, not the first mention — every one of these is named in a
        # comment paragraphs earlier, and matching that measures the prose instead of the code.
        for step, call in (("mksquashfs", "mksquashfs / "),
                           ("dracut", "dracut --force"),
                           ("grub-mkrescue", "grub-mkrescue -o")):
            with self.subTest(step=step):
                self.assertIn(call, self.body, "%s is never actually run" % step)
                i = self.body.index(call)
                after = self.body[i:i + 900]
                self.assertIn("return", after, "%s does not stop the build when it fails" % step)
                self.assertTrue("_lcd_fail" in after or "COLOR_YELLOW" in after,
                                "%s fails without saying so" % step)


    def test_the_machine_id_is_blanked(self):
        """A duplicated machine-id gives every live boot the same identity, which breaks journald,
        DHCP leases and systemd-boot's own /boot layout. Empty means "first boot" to systemd."""
        self.assertIn("etc/machine-id", self.code)

    def test_it_is_reachable_from_the_menu(self):
        """A function nothing calls is a feature nobody has.

        The LABEL is not pinned word for word — it has already been rewritten once ("Build a
        LiveCD" -> "Build an installable ISO of this system") and this test failed for that alone,
        which is a test that cries about prose while saying nothing about reachability. What matters
        is that the menu offers a numbered entry that mentions an ISO and that the branch behind it
        calls the function."""
        tweaks = _fn("tweaks")
        self.assertIn("liveCD", tweaks)
        entries = [l for l in tweaks.splitlines() if "ISO" in l and "echo" in l]
        self.assertTrue(entries, "no menu line offers to build an ISO")
        import re as _re
        self.assertTrue(_re.search(r"\[\d\]", entries[0]),
                        "the ISO entry has no number to type: %s" % entries[0])


if __name__ == "__main__":
    unittest.main()


class TheFailureCanActuallyBeREAD(unittest.TestCase):
    """"i can't read the error generating a live cd because it goes back to the menu."

    Every failure printed a line and waited for a keypress — and then the menu redraws with `clear`,
    so the message vanishes the instant somebody presses the key they were just told to press. On a
    build that runs for half an hour and prints hundreds of lines, the one that matters has usually
    scrolled off before that anyway. A transcript is the only thing that survives both, and it has
    to be opened at the TOP of the function: the earliest failures — a missing tool, a
    squashfs-tools without zstd — happen long before anybody has been asked where the ISO goes, so a
    log living under the output directory cannot record them.
    """

    @classmethod
    def setUpClass(cls):
        cls.body = _fn("liveCD")

    def test_there_is_one_log_and_it_is_opened_before_anything_can_fail(self):
        self.assertIn('LOG="/var/tmp/pc-livecd.log"', self.body)
        # Before the first thing that can stop the build: the tool preflight.
        self.assertLess(self.body.index('LOG="/var/tmp/pc-livecd.log"'),
                        self.body.index("command -v mksquashfs"),
                        "the log is opened after the first step that can fail, so that step's "
                        "failure is exactly the one it cannot record")

    def test_every_way_out_says_where_to_read_it(self):
        """A path that prints and returns without naming the log is a path whose message is gone the
        moment the menu redraws."""
        self.assertIn("_lcd_fail()", self.body)
        self.assertIn("$LOG", self.body)
        # No failure path may still be the old print-and-wait shape.
        stray = [l.strip() for l in self.body.splitlines()
                 if "COLOR_YELLOW" in l and "failed" in l.lower() and "_lcd_fail" not in l]
        self.assertEqual(stray, [], "these failures do not go through _lcd_fail: %s" % stray)

    def test_no_failure_is_judged_by_a_pipelines_own_status(self):
        """`if ! cmd | tee` tests TEE, which succeeds whatever happened upstream — the same trap the
        /logs board paid for, and here it would turn a failure into a silent success that goes on to
        build half an ISO. Teed commands must be judged by PIPESTATUS."""
        bad = [l.strip() for l in self.body.splitlines()
               if "| tee" in l and l.strip().startswith("if ")]
        self.assertEqual(bad, [], "judged by the pipeline's own status: %s" % bad)
        for cmd in ("emerge -n $NEED", "--newuse sys-fs/squashfs-tools", "grub-mkrescue -o"):
            i = self.body.index(cmd)
            after = self.body[i:i + 400]
            if "| tee" in after:
                self.assertIn("PIPESTATUS", after,
                              "%s is teed and its real exit status is never read" % cmd)
