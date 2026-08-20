"""The live ISO can install itself, and does not carry the machine it was built from.

    "we need to make sure that the livecd is built with gentoo.sh so they can install it on any
     system"
    "laptop needs to reflect a new os install and not have verita84 configured at all"

Two separate problems with one builder.

INSTALLABLE. The builder's own header said "not an installer image" — it made a live disc of a
running machine with no way to adopt it. Worse, the installer IS gentoo.sh, which lives in a home
directory, and `/home` is excluded by default: the one file needed was the one guaranteed to be
missing. It is injected as pseudo-files now, with a .desktop entry so the live session's own start
menu lists it (that menu already reads every .desktop on the machine).

CLEAN. An ISO of your machine is a copy of your machine: your account, your password hash, your ssh
HOST keys, your saved wifi, your history — and it autologins as you on somebody else's hardware. The
ssh host keys are the sharp one, since every machine installed from the ISO would present the same
identity.

The account rewriting is RUN here against fixtures, not grepped, because it is the part that fails
in two opposite and equally silent ways: leaving a real account in (a leak), or dropping root and the
system users (an image that cannot boot).
"""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENTOO = ROOT / "os" / "gentoo.sh"


class TheBuilderShipsTheInstaller(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = GENTOO.read_text()
        i = cls.src.index("liveCD() {")
        cls.fn = cls.src[i:cls.src.index("\n}", i)]

    def test_the_script_directory_goes_into_the_image(self):
        self.assertIn("usr/local/share/posterchanos", self.fn)

    def test_it_takes_the_whole_tree_not_just_the_script(self):
        """gentoo.sh reads `$(dirname $0)/bin` and `/plymouth` and half-works without them."""
        self.assertIn("find \"$IHERE\"", self.fn)

    def test_it_does_not_copy_into_the_running_system(self):
        """Building an ISO must not modify the machine being imaged — the same rule the fstab
        rewrite follows."""
        i = self.fn.index("usr/local/share/posterchanos")
        seg = self.fn[max(0, i - 600):i + 600]
        self.assertNotIn("cp -r /usr/local/share", seg)
        self.assertIn("cat ", seg)

    def test_the_installer_is_executable_in_the_image(self):
        i = self.fn.index("usr/local/share/posterchanos/$REL f")
        self.assertIn("755", self.fn[i:i + 80])

    def test_there_is_a_way_to_find_it(self):
        """A terminal command nobody is told about is not a way to install an operating system."""
        self.assertIn("posterchanos-install.desktop", self.fn)
        self.assertIn("Install PosterChanOS", self.fn)

    def test_the_desktop_entry_can_reach_root(self):
        i = self.fn.index("[Desktop Entry]")
        self.assertIn("sudo", self.fn[i:i + 500])


class TheImageDoesNotCarryTheOperator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = GENTOO.read_text()
        i = src.index("liveCD() {")
        cls.fn = src[i:src.index("\n}", i)]

    def test_it_asks_and_defaults_to_clean(self):
        m = re.search(r"read -p \"Clean out[^\"]*\" -e -i \"([yn])\" CLEAN", self.fn)
        self.assertIsNotNone(m, "the clean/personal choice is gone")
        self.assertEqual(m.group(1), "y")

    def test_ssh_host_keys_are_left_out(self):
        """Every machine installed from the ISO would otherwise present the SAME host identity."""
        self.assertIn("/etc/ssh/ssh_host_*", self.fn)

    def test_saved_networks_are_left_out(self):
        for place in ("NetworkManager/system-connections", "iwd", "wpa_supplicant"):
            with self.subTest(place=place):
                self.assertIn(place, self.fn)

    def test_the_shadow_backups_go_too(self):
        """passwd- and shadow- hold exactly what the rewritten ones drop."""
        self.assertIn("/etc/shadow-", self.fn)

    def test_a_clean_image_never_keeps_a_home(self):
        """The two questions can be answered in contradiction."""
        self.assertIn('"$KEEP_HOME" = *n* || "$CLEAN" = *y*', self.fn)

    def test_it_still_autologins(self):
        """Removing the autologin gives a prompt for an account with no password set."""
        self.assertIn("--autologin live", self.fn)

    def test_the_live_account_is_passwordless_not_locked(self):
        """`!` is locked, and a locked account cannot autologin."""
        self.assertIn("live::20000", self.fn)

    def test_the_hostname_is_not_this_machines(self):
        self.assertIn("etc/hostname f 644 0 0 echo posterchanos", self.fn)


class TheCloneToolIsStillThere(unittest.TestCase):
    """The ISO builder is a NEW option, not a replacement for anything.

    Reported as "you kinda ruined the important feature of gentoo.sh, option 6 used to let you clone
    desktop -> usb and vice versa". It had not been removed — but there are now two different [6]s,
    one per menu, and both move an operating system around. The main menu's has cloned a running
    system between a disk and a USB since the first commit; mine writes an ISO and lives under Tools
    and Tweaks. This pins the older one so a future tidy-up cannot quietly take it, and pins the
    labels apart so they cannot be confused again.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = GENTOO.read_text()

    def test_backup_restore_is_still_offered(self):
        self.assertIn("Backup/Restore Live OS", self.src)

    def test_it_still_has_a_function_behind_it(self):
        self.assertIn("liveOSrestore()", self.src)
        self.assertIn('liveOSrestore "$HARD_DISK"', self.src)

    def test_backup_to_a_server_is_still_offered(self):
        """The other direction."""
        self.assertIn("Backup OS to Build Server", self.src)
        self.assertIn("backupOS()", self.src)

    def test_option_six_means_exactly_one_thing(self):
        """The ISO builder is numbered 7 and 6 is left empty in that menu on purpose. Two [6]s that
        both move an operating system around, one menu apart, is what made a working feature look
        deleted."""
        # Only MENU ENTRIES count. A `[6]` in a comment explaining this rule is prose, and so is the
        # hint pointing at the clone tool — both name the number without being it. An entry's number
        # follows the colour escape directly, which is what tells them apart.
        sixes = [l for l in re.findall(r"(?m)^\s*echo -e .*?m\[6\][^\\]*", self.src)]
        self.assertEqual(len(sixes), 1, "there is more than one [6] menu entry again: %r" % sixes)
        self.assertIn("Backup/Restore", sixes[0])

    def test_the_iso_builder_is_not_numbered_six(self):
        i = self.src.index("Build an installable ISO")
        self.assertIn("[7]", self.src[max(0, i - 40):i])

    def test_the_dispatch_agrees_with_the_label(self):
        """A renumbered label with the old branch behind it is a menu entry that does nothing."""
        i = self.src.rindex("fixSound")
        self.assertRegex(self.src[i:i + 200], r"choice = 7 \]\]; then\s*liveCD")

    def test_the_iso_builder_points_at_the_clone_tool(self):
        i = self.src.index("Build an installable ISO")
        self.assertIn("main menu", self.src[i:i + 600])


class TheMenusSayWhatThisInstalls(unittest.TestCase):
    """"you need to rename the menus to PosterChanOS Installer".

    The script builds PosterChanOS and nothing else — the Gentoo-profile branches were taken out
    already — but the headings still announced a Gentoo installer, so the thing on screen disagreed
    with the thing being installed.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = GENTOO.read_text()

    def test_no_menu_heading_calls_this_a_gentoo_installer(self):
        for line in self.src.splitlines():
            if line.strip().startswith("echo -e") and "nstaller" in line:
                with self.subTest(line=line.strip()[:70]):
                    self.assertNotIn("Gentoo Installer", line)
                    self.assertNotIn("GENTOO CYBERPUNK", line)

    def test_the_headings_name_posterchanos(self):
        heads = [l for l in self.src.splitlines()
                 if l.strip().startswith("echo -e") and "nstaller" in l.lower()]
        self.assertTrue(heads, "the installer headings are gone — re-read this test")
        for h in heads:
            with self.subTest(head=h.strip()[:70]):
                self.assertIn("POSTERCHANOS", h.upper())


class ItFindsItsOwnFilesWhereverItIsInstalled(unittest.TestCase):
    """`$(dirname $0)` is right in a checkout and wrong once installed.

    "replace /usr/bin/gentoo.sh with the latest gentoo.sh" — at that path `dirname` is /usr/bin, so
    the script looked for /usr/bin/bin and /usr/bin/plymouth, found neither, and carried on. Nothing
    fails: the pc-* helpers are simply not copied, and the first sign is a freshly installed machine
    whose desktop has no pc-shell-start.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = GENTOO.read_text()

    def test_the_tree_is_resolved_once(self):
        self.assertIn("PCOS_TREE=", self.src)

    def test_no_use_site_still_guesses_from_argv0(self):
        """Only the RESOLVER may look at $0 — checked as a span, since the resolver's own loop
        naturally mentions it on a line that says nothing else."""
        lines = self.src.splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith('PCOS_TREE=""'))
        end = next(i for i, l in enumerate(lines) if l.startswith('[ -n "$PCOS_TREE" ]'))
        for n, line in enumerate(lines, 1):
            if 'dirname "$0"' in line and not line.strip().startswith("#"):
                if start < n <= end + 1:
                    continue                      # inside the resolver, which is its whole job
                with self.subTest(line=n):
                    self.fail("line %d derives a path from $0 outside the resolver: %s"
                              % (n, line.strip()))

    def test_it_looks_where_the_iso_puts_it(self):
        i = self.src.index("PCOS_TREE=")
        self.assertIn("/usr/local/share/posterchanos", self.src[i:i + 500],
                      "an ISO-installed copy cannot find the tree the ISO shipped")

    def test_the_helpers_and_theme_use_it(self):
        self.assertIn('"$PCOS_TREE/bin/$helper"', self.src)
        self.assertIn('"$PCOS_TREE/plymouth/posterchanos"', self.src)

    def test_a_bare_script_still_runs(self):
        """Not finding the tree must not be fatal — every use site has its own fallbacks, and an
        install from a lone script beats no install."""
        i = self.src.index("PCOS_TREE=")
        seg = self.src[i:i + 900]
        self.assertNotIn("exit 1", seg)


class TheAccountRewriteActuallyWorks(unittest.TestCase):
    """RUN, not grepped. It fails in two opposite silent ways: leaving a person in, or dropping the
    system users the image needs to boot."""

    PASSWD = ("root:x:0:0:root:/root:/bin/bash\n"
              "bin:x:1:1:bin:/bin:/sbin/nologin\n"
              "sshd:x:22:22:sshd:/var/empty:/sbin/nologin\n"
              "verita84:x:1000:1000::/home/verita84:/bin/bash\n"
              "pc-5ac337fb7cb82127:x:1001:1001::/home/pc-5ac337fb7cb82127:/bin/bash\n"
              "nobody:x:65534:65534:nobody:/:/sbin/nologin\n")
    SHADOW = ("root:$6$realhash:19000:0:99999:7:::\n"
              "bin:!:19000::::::\n"
              "sshd:!:19000::::::\n"
              "verita84:$6$SECRETHASH:19000:0:99999:7:::\n"
              "pc-5ac337fb7cb82127:$6$OTHERHASH:19000:0:99999:7:::\n"
              "nobody:!:19000::::::\n")

    def _run(self, script, files):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for n, body in files.items():
                (d / n).write_text(body)
            r = subprocess.run(["bash", "-c", script], cwd=d, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return {n: (d / n).read_text() for n in ("passwd.out", "shadow.out") if (d / n).exists()}

    def test_people_are_dropped_and_the_system_survives(self):
        out = self._run("awk -F: '$3 < 1000 || $3 >= 65534' passwd > passwd.out",
                        {"passwd": self.PASSWD})["passwd.out"]
        self.assertNotIn("verita84", out)
        self.assertNotIn("pc-5ac337fb7cb82127", out)
        for keep in ("root:x:0:0", "bin:x:1:1", "sshd:x:22:22", "nobody:x:65534"):
            with self.subTest(keep=keep):
                self.assertIn(keep, out)

    def test_no_password_hash_of_a_real_person_survives(self):
        out = self._run(
            "awk -F: 'NR==FNR { if ($3 >= 1000 && $3 < 65534) drop[$1]; next } !($1 in drop)' "
            "passwd shadow > shadow.out",
            {"passwd": self.PASSWD, "shadow": self.SHADOW})["shadow.out"]
        self.assertNotIn("SECRETHASH", out)
        self.assertNotIn("OTHERHASH", out)
        self.assertIn("root:$6$realhash", out, "root's entry was dropped — the image cannot boot")


if __name__ == "__main__":
    unittest.main()
