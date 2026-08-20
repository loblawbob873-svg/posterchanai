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
