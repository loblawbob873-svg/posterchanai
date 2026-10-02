"""Giving a Nostr identity a Unix account, RUN against stubbed system tools.

PosterChanOS logs in with a KEY. Home directories and permissions are a Unix idea, so somebody
signing in for the first time needs an account made for them before they have anywhere to put
anything — and on a machine anyone may sign in to, "anyone" is the threat model as well as the
feature.

Three things here are worth more than the rest, and each is run rather than read:

  * THE NAME MAPPING. An npub is 63 characters and a Linux user name is at most 32, so it must be
    shortened — and a TRUNCATION hands one person another person's account the first time two keys
    share a prefix. The name carries a hash of the whole key instead.
  * THE MODE. `useradd` honours /etc/login.defs, which is 0755 on most systems, so every user's
    files would be readable by every other user on the machine. That is exactly what separate
    accounts were for.
  * THE ARGUMENT IS UNTRUSTED. This runs as root and its input comes from a login screen.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "os", "bin", "pc-provision-user")

NPUB_A = "npub1fdtthaqujtjcd6yfy7kt0zpkadyl9vvypq00s5nztnmche74d0tqv6uwwr"
NPUB_B = "npub19q5ezl4qrhy4dt5cnfvsxpxc7qzqmkakqzp0ka2qy2j0nspq3fmqgxmzpr"

STUBS = {
    "runuser": '#!/bin/sh\nshift 3\nexec "$@"\n',
    # Records its argv, and pretends the account now exists by appending to a fake passwd.
    # Honours -u like the real one: the account gets the uid it was asked for, else 1500.
    "useradd": '#!/bin/sh\necho "useradd $*" >> "$PC_LOG"\nuid=1500\nprev=\n'
               'for a in "$@"; do [ "$prev" = "-u" ] && uid="$a"; prev="$a"; last="$a"; done\n'
               'echo "$last:x:$uid:$uid::$PC_HOME_ROOT/$last:/bin/bash" >> "$PC_PASSWD"\n'
               'mkdir -p "$PC_HOME_ROOT/$last"\nexit 0\n',
    "id": '#!/bin/sh\nif [ "$1" = "-u" ]; then\n'
          '  line=$(grep "^$2:" "$PC_PASSWD" 2>/dev/null) || exit 1\n'
          '  echo "$line" | cut -d: -f3\n  exit 0\nfi\nexit 1\n',
    # The home folder's owner. PC_HOME_UID is what an upgraded disk's /home carries; otherwise the
    # owner is whatever account the passwd says owns it (a home this run just made).
    "stat": '#!/bin/sh\nd=$3\nu=$(basename "$d")\nif [ -n "$PC_HOME_UID" ]; then echo "$PC_HOME_UID"; '
            'else grep "^$u:" "$PC_PASSWD" 2>/dev/null | cut -d: -f3 | grep . || echo 0; fi\n',
    "groupadd": '#!/bin/sh\necho "groupadd $*" >> "$PC_LOG"\nexit 0\n',
    # By name or by NUMBER, as the real one answers: a uid/gid lookup is what decides whether an
    # upgraded home's owner is free to take back. PC_TAKEN_IDS lists numbers some OTHER account holds.
    "getent": '#!/bin/sh\ncase "$2" in *[!0-9]*) ;; *) case " $PC_TAKEN_IDS " in *" $2 "*) echo "other:x:$2:"; exit 0;; esac; '
              'if [ "$1" = passwd ]; then awk -F: -v u="$2" \'$3==u{print;f=1}END{exit !f}\' "$PC_PASSWD" 2>/dev/null; exit; fi; exit 2;; esac\n'
              'if [ "$1" = "passwd" ]; then grep "^$2:" "$PC_PASSWD" 2>/dev/null; '
              'else echo "$2:x:100:"; fi\n',
    "gpasswd": '#!/bin/sh\necho "gpasswd $*" >> "$PC_LOG"\nexit 0\n',
    "chown": '#!/bin/sh\necho "chown $*" >> "$PC_LOG"\nexit 0\n',
}


@unittest.skipIf(not os.path.exists(SCRIPT), "no provisioner here")
class Provision(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bin = os.path.join(self.dir, "bin")
        os.makedirs(self.bin)
        for name, body in STUBS.items():
            p = os.path.join(self.bin, name)
            with open(p, "w") as fh:
                fh.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.passwd = os.path.join(self.dir, "passwd")
        open(self.passwd, "w").close()
        self.log = os.path.join(self.dir, "log")
        self.homes = os.path.join(self.dir, "home")
        os.makedirs(self.homes)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_it(self, npub, **extra):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                   PC_LOG=self.log, PC_PASSWD=self.passwd, PC_HOME_ROOT=self.homes,
                   PC_STATE_ROOT=os.path.join(self.dir, "state"),
                   PC_SUDOERS_ROOT=os.path.join(self.dir, "sudoers"))
        env.pop("PC_HOME_UID", None); env.pop("PC_TAKEN_IDS", None)
        env.update(extra)
        return subprocess.run(["bash", SCRIPT, npub], capture_output=True, text=True,
                              timeout=60, env=env)

    def out(self, r):
        return dict(l.split("=", 1) for l in r.stdout.strip().splitlines() if "=" in l)

    def test_it_makes_an_account_and_reports_it(self):
        r = self.run_it(NPUB_A)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        o = self.out(r)
        self.assertTrue(o["user"].startswith("pc-"))
        self.assertEqual(o["home"], os.path.join(self.homes, o["user"]))

    def test_the_name_comes_from_the_whole_key_not_a_prefix(self):
        """Two keys sharing a prefix must not share an account. A truncation would hand one person
        another person's files, and it would do it silently."""
        a = self.out(self.run_it(NPUB_A))["user"]
        self.setUp()
        b = self.out(self.run_it(NPUB_B))["user"]
        self.assertNotEqual(a, b)
        # ...and the same key must resolve to the same account on every machine, or somebody's home
        # directory changes name under them.
        self.setUp()
        self.assertEqual(self.out(self.run_it(NPUB_A))["user"], a)

    def test_the_name_is_a_legal_linux_user_name(self):
        o = self.out(self.run_it(NPUB_A))
        self.assertLessEqual(len(o["user"]), 32, o["user"])
        self.assertRegex(o["user"], r"^[a-z_][a-z0-9_-]*$")

    def test_the_home_is_really_0700_on_disk(self):
        """useradd honours /etc/login.defs, which is 0755 on most systems — every user's files
        readable by every other user, which is what separate accounts were for. Checked as the mode
        the directory ACTUALLY ends up with, not as a line in the script."""
        home = self.out(self.run_it(NPUB_A))["home"]
        self.assertTrue(os.path.isdir(home), home)
        self.assertEqual(stat.S_IMODE(os.stat(home).st_mode), 0o700,
                         "another user on this machine can read this person's files")

    def test_the_identity_is_recorded_and_private(self):
        home = self.out(self.run_it(NPUB_A))["home"]
        marker = os.path.join(home, ".posterchan-npub")
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(open(marker).read().strip(), NPUB_A)
        self.assertEqual(stat.S_IMODE(os.stat(marker).st_mode), 0o600)

    def test_it_is_idempotent(self):
        """Run on every login, not only the first — "was this account already made" is not a
        question the caller should have to remember the answer to."""
        a = self.run_it(NPUB_A)
        b = self.run_it(NPUB_A)
        self.assertEqual(b.returncode, 0, b.stderr[-500:])
        self.assertEqual(self.out(a)["user"], self.out(b)["user"])
        self.assertEqual(open(self.log).read().count("useradd "), 1,
                         "it made the account twice")

    def test_a_non_npub_is_refused(self):
        """This runs as ROOT and its argument comes from a login screen."""
        for bad in ("", "root", "npub1$(touch /tmp/pwned)", "../../etc/passwd",
                    "nsec1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq",
                    "npub1" + "z" * 100, "npub1SHOUTING"):
            r = self.run_it(bad)
            self.assertNotEqual(r.returncode, 0, f"{bad!r} was accepted")
            # It must say why on stderr — "usage" for nothing at all, "refusing" for input that
            # looked like an attempt. Either way it must not have made an account.
            self.assertTrue(r.stderr.strip(), f"{bad!r} failed silently")
            self.assertNotIn("useradd", open(self.log).read() if os.path.exists(self.log) else "",
                             f"{bad!r} created an account before being refused")

    def test_it_refuses_to_adopt_an_account_it_did_not_make(self):
        """The generated name is ours by construction, but if one ever collides with an account
        somebody else has, taking it over hands them another person's files."""
        o = self.out(self.run_it(NPUB_A))
        with open(self.passwd, "w") as fh:
            fh.write(f"{o['user']}:x:1000:1000::/home/someone-else:/bin/bash\n")
        r = self.run_it(NPUB_A)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing", r.stderr.lower())

    def test_only_the_first_person_is_admin(self):
        """The first key claims ownership; a later key must not inherit administration."""
        first = self.run_it(NPUB_A)
        second = self.run_it(NPUB_B)
        self.assertEqual(self.out(first)["admin"], "true")
        self.assertEqual(self.out(second)["admin"], "false")
        log = open(self.log).read()
        self.assertEqual(log.count("gpasswd -a pc-") >= 1, True)
        self.assertEqual(log.count(" wheel"), 1, log)
        rule = open(os.path.join(self.dir, "sudoers", "posterchan-admin")).read()
        self.assertIn(self.out(first)["user"], rule)
        self.assertNotIn(self.out(second)["user"], rule)
        self.assertIn("NOPASSWD: ALL", rule,
                      "a key-backed account has no Unix password and could never use sudo")
        for needed in ("audio", "video", "input"):
            self.assertIn(needed, log, f"a session cannot work without the {needed} group")


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(not os.path.exists(SCRIPT), "no provisioner here")
class UpgradedHome(Provision):
    """"Upgrade an installed PosterChanOS" keeps /home and replaces /etc/passwd: the person's files are
    on disk under a uid that no account has any more. Signing in again must give those files back to
    them -- by taking the uid back, or by handing the files over -- never leave them owned by nobody."""

    def _upgraded_home(self, npub):
        import hashlib
        name = "pc-" + hashlib.sha256(npub.encode()).hexdigest()[:16]
        os.makedirs(os.path.join(self.homes, name, "Documents"))
        return name

    def test_the_old_uid_is_taken_back_when_it_is_free(self):
        self._upgraded_home(NPUB_A)
        r = self.run_it(NPUB_A, PC_HOME_UID="1001")
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        log = open(self.log).read()
        self.assertIn("-u 1001", log, "the account was made under a NEW uid; the upgraded home is owned by nobody")
        self.assertIn("groupadd -g 1001", log)
        self.assertNotIn("chown -R", log, "the uid matched -- nothing needed handing over")

    def test_when_the_old_uid_is_taken_the_files_are_handed_over(self):
        self._upgraded_home(NPUB_A)
        r = self.run_it(NPUB_A, PC_HOME_UID="1001", PC_TAKEN_IDS="1001")
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        log = open(self.log).read()
        self.assertNotIn("-u 1001", log, "took a uid another account holds -- that hands THEM these files")
        self.assertIn("chown -R", log, "the files stayed under a uid that now belongs to somebody else")
