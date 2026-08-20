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
import tempfile
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


class TheTerminalIsGivenBack(unittest.TestCase):
    """"after leaving gentoo.sh, terminal is messed up again. adding extra characters as I type."

    The script drives the terminal hard and never gave any of it back. `read -e` turns on readline,
    which enables BRACKETED PASTE and application cursor keys; `clear` and the colour codes do their
    own work. Bash restores what IT set when a normal interactive shell exits — but a script quit
    part-way, exited from inside a menu branch, or killed while a `read` is pending leaves those
    modes switched on in the terminal it was running in. What is left is a tty that echoes paste
    markers and duplicates what you type, which is what "adding extra characters" is.

    `stty sane` cannot do this alone: bracketed paste and application cursor keys are the EMULATOR's
    state, not the line discipline's, and stty knows nothing about either.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = open(SH).read()

    def test_something_restores_the_tty(self):
        self.assertIn("stty sane", self.src,
                      "nothing restores echo or canonical mode when the script ends")

    def test_the_emulator_modes_are_switched_off_too(self):
        """2004 is bracketed paste — the one that duplicates typed input in an emulator that is left
        holding it."""
        self.assertIn("?2004l", self.src, "bracketed paste is never switched back off")
        self.assertIn("?1l", self.src, "application cursor keys are never switched back off")

    def test_it_runs_however_the_script_ends(self):
        """Falling off the end is the case that already worked. The ones that did not are an `exit`
        from a menu branch and a Ctrl-C during a `read`."""
        line = [l for l in self.src.splitlines() if l.strip().startswith("trap ")]
        self.assertTrue(line, "there is no trap at all")
        t = line[0]
        for sig in ("EXIT", "INT", "TERM"):
            self.assertIn(sig, t, "the restore does not run on %s: %s" % (sig, t))

    def test_it_cannot_itself_become_the_failure(self):
        """With no terminal — a pipe, a cron job — there is nothing to restore, and a restore that
        errors on the way out would be a new failure mode bolted to every exit path."""
        body = _fn("_pc_tty_restore")
        self.assertIn("-t 0", body, "it does not check there is a terminal")
        self.assertIn("2>/dev/null", body, "it can print an error on the way out")


class TheZstdProbeAsksByDoing(unittest.TestCase):
    """"i already recompiled with zstd and now your version tried to recompile something and then
    says it rebuilt and still has no zstd."

    The probe was `mksquashfs -help | grep -qw zstd`, and that is wrong on any current
    squashfs-tools: 4.6 turned `-help` into a short summary and moved the compressor list behind
    `-help-all` / `-help-comp`. On a machine that had ALREADY been rebuilt with the flag it found
    nothing, rebuilt for no reason, ran the identical probe, found nothing again, and announced the
    rebuild had failed. Every part of that was the probe.

    Parsing help text is guessing at an interface that is allowed to change. Compressing one file is
    not. This RUNS the shipped function against two stub tools — one that writes a zstd image while
    printing 4.6-style help that never mentions compressors, and one that refuses zstd — because a
    test that also read the help text would agree with the bug.
    """

    GOOD = ('#!/bin/bash\n'
            'if [[ "$1" == "-help" ]]; then echo "SYNTAX: mksquashfs source dest [options]";'
            ' echo "Run mksquashfs -help-all for full help"; exit 0; fi\n'
            ': > "$2"; exit 0\n')
    BAD = ('#!/bin/bash\n'
           'if [[ "$1" == "-help" ]]; then echo "SYNTAX: mksquashfs source dest [options]"; exit 0; fi\n'
           'for a in "$@"; do if [[ "$a" == "zstd" ]]; then'
           ' echo "Compressor \\"zstd\\" is not supported!" >&2; exit 1; fi; done\n'
           ': > "$2"; exit 0\n')

    def _ask(self, stub):
        import os as _os
        import stat as _stat
        fn = _fn("_pc_mksquashfs_zstd")
        with tempfile.TemporaryDirectory() as d:
            tool = os.path.join(d, "mksquashfs")
            with open(tool, "w") as fh:
                fh.write(stub)
            _os.chmod(tool, _os.stat(tool).st_mode | _stat.S_IEXEC)
            env = dict(_os.environ, PATH=d + os.pathsep + _os.environ.get("PATH", ""))
            script = "_pc_mksquashfs_zstd() {\n%s\n}\nLOG=/dev/null\n_pc_mksquashfs_zstd\n" % (
                fn.split("{", 1)[1].rsplit("}", 1)[0])
            r = subprocess.run(["bash", "-c", script], env=env, capture_output=True)
            return r.returncode == 0

    def test_a_tool_that_can_write_zstd_is_recognised(self):
        """Even though its `-help` never mentions a compressor — which is the 4.6+ shape, and the
        exact case that was misread as a failed rebuild."""
        self.assertTrue(self._ask(self.GOOD),
                        "a working zstd tool is reported as broken, so the build rebuilds it for "
                        "ever and never gets past the check")

    def test_a_tool_that_cannot_is_not(self):
        self.assertFalse(self._ask(self.BAD))

    def test_nothing_reads_the_help_text_any_more(self):
        """The old probe left in place beside the new one would still be the thing that decides."""
        body = _fn("liveCD")
        self.assertNotIn("-help 2>&1 | grep -qw zstd", body,
                         "the help-text probe is still here")
        self.assertIn("_pc_mksquashfs_zstd", body)


class TheDefaultIsTheHomeDirectory(unittest.TestCase):
    """"make the default dir for livecd your homedir."

    It was /var/tmp/livecd — a fine place for a build tree and a strange place to go hunting for an
    ISO you just made. `$HOME` alone is the wrong question, because this runs under sudo and `$HOME`
    is then root's; `$SUDO_USER` names the person who actually typed the command.
    """

    @classmethod
    def setUpClass(cls):
        cls.body = _fn("liveCD")

    def test_it_defaults_under_the_invoking_users_home(self):
        self.assertIn("SUDO_USER", self.body,
                      "under sudo the default would be /root, which is nobody's home directory")
        self.assertIn("getent passwd", self.body)

    def test_it_still_has_somewhere_to_go_when_that_cannot_be_written(self):
        """A default that cannot be used is not a default."""
        self.assertIn("/var/tmp/livecd", self.body)
        self.assertIn("-w", self.body)

    def test_the_destination_is_printed_before_anything_slow(self):
        """Ending up in the home directory by accident was the ORIGINAL bug; choosing it is fine, and
        the difference is whether it is said out loud first."""
        i = self.body.index("ISO:  $ISO")
        j = self.body.index("mksquashfs / ")
        self.assertLess(i, j, "the destination is announced after the filesystem is packed")


class TheLiveInitramfsStartsFromNothing(unittest.TestCase):
    """"live cd error: dracut failed the iso would not boot / module systemd-cryptsetup depends on
    module crypt."

    Two problems wearing one hat, and only the first one announced itself.

    An encrypted install writes `add_dracutmodules+=" crypt systemd-cryptsetup dm rootfs-block "`
    into /etc/dracut.conf, and dracut reads that file whatever the command line says. So the config
    ADDED systemd-cryptsetup while this build OMITTED crypt, and dracut refused the contradiction —
    correctly. Omitting crypt is right: a live image boots from a squashfs on an ISO, not a LUKS
    disk.

    The second problem is the one nobody would have noticed. That same file carries
    `install_items+=" /boot/unlock.sh /boot/keyfile.key "` — the key that unlocks THIS machine's disk
    with no password. Inherited, every ISO built on an encrypted install would have shipped that key
    inside its initramfs, on a disc meant to be handed to somebody, defeating the whole "clean out
    this machine's accounts and secrets" pass a few dozen lines above.
    """

    @classmethod
    def setUpClass(cls):
        cls.body = _fn("liveCD")

    def test_it_does_not_read_the_host_config(self):
        i = self.body.index("dracut --force")
        call = self.body[i:i + 500]
        self.assertIn("--conf /dev/null", call,
                      "the live initramfs inherits /etc/dracut.conf, which on an encrypted install "
                      "adds systemd-cryptsetup and installs the machine's LUKS keyfile")
        self.assertIn("--confdir", call,
                      "dracut.conf.d is read even when --conf is redirected")

    def test_the_confdir_it_is_pointed_at_is_one_we_made_empty(self):
        self.assertIn('mkdir -p "$WORK/dracut.conf.d"', self.body)
        i = self.body.index('mkdir -p "$WORK/dracut.conf.d"')
        self.assertLess(i, self.body.index("dracut --force"),
                        "the directory is created after it is used")

    def test_the_contradiction_is_removed_rather_than_silenced(self):
        """`--omit systemd-cryptsetup` would have stopped the error and left the keyfile in the
        image, which is the worse of the two failures and the silent one."""
        i = self.body.index("dracut --force")
        call = self.body[i:i + 500]
        self.assertNotIn("systemd-cryptsetup", call,
                         "the module is omitted by name, which hides the config leak instead of "
                         "ending it")

    def test_dracuts_own_output_reaches_the_log(self):
        """"dracut failed" with no reason is the report. Its output is what names the module."""
        i = self.body.index("dracut --force")
        call = self.body[i:i + 600]
        self.assertIn('tee -a "$LOG"', call)
        self.assertIn("PIPESTATUS", self.body[i:i + 900])


class ChangingTheDiskPassword(unittest.TestCase):
    """"add menu option/function in tools part of gentoo.sh to change Luks disk password."

    `cryptsetup luksChangeKey` on the slot the old password opens — no reformat, no re-encrypt, no
    reboot. The data is encrypted with a master key that never changes and a LUKS password only
    unlocks that key, so this is a header write of a few kilobytes and is instant on a full disk.

    The failure mode here is not an error message, it is a machine that will not boot, so what is
    checked before anything is written matters more than the change itself.
    """

    @classmethod
    def setUpClass(cls):
        cls.body = _fn("changeDiskPassword")

    def test_it_is_reachable_from_the_tools_menu(self):
        tweaks = _fn("tweaks")
        self.assertIn("changeDiskPassword", tweaks, "a function nothing calls is a feature nobody has")
        entries = [l for l in tweaks.splitlines() if "encryption password" in l and "echo" in l]
        self.assertTrue(entries, "no menu line offers it")
        self.assertTrue(re.search(r"\[\d\]", entries[0]), "no number to type: %s" % entries[0])

    def test_it_refuses_anything_that_is_not_luks(self):
        """Detection guesses the layout from /etc/disk and picks the second partition. On a machine
        partitioned differently that is somebody's data, and a LUKS header written over it is
        unrecoverable."""
        self.assertIn("cryptsetup isLuks", self.body,
                      "it would write a header over whatever detection happened to name")

    def test_the_old_password_is_checked_before_it_matters(self):
        """--test-passphrase opens nothing and writes nothing; it answers whether the password has a
        slot, so a typo is reported as a typo rather than as a cryptsetup exit code."""
        self.assertIn("--test-passphrase", self.body)
        self.assertLess(self.body.index("--test-passphrase"), self.body.index("luksChangeKey"))

    def test_the_new_one_is_typed_twice_and_is_not_empty(self):
        """There is no "forgot password" for this."""
        self.assertEqual(self.body.count("read -rsp"), 3,
                         "expected the old password and the new one twice")
        self.assertIn('"$NEW1" != "$NEW2"', self.body)
        self.assertIn('-z "$NEW1"', self.body)

    def test_nothing_is_echoed(self):
        """`read -s`, so it never reaches the screen, the environment or a file."""
        for line in self.body.splitlines():
            if "read -" in line and ("password" in line.lower() or "OLD" in line or "NEW" in line):
                with self.subTest(line=line.strip()):
                    self.assertIn("-rsp", line, "a password is read without -s: %s" % line.strip())

    def test_the_hands_free_keyfile_is_mentioned(self):
        """The keyfile is a SEPARATE slot and is deliberately untouched — the machine goes on booting
        without a prompt and the typed password changes. "I changed my disk password and it still
        boots without asking" is otherwise a reasonable thing to be alarmed by."""
        self.assertIn("keyfile", self.body)
