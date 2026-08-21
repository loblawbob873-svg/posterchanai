"""`update-posterchan` — one command that updates the app and the session together.

Asked for as "a bash alias to update posterchan". It is a file in /usr/local/bin instead, because an
alias exists only inside an interactive bash: invisible to `sudo`, to a .desktop entry, to a script,
and over ssh, which are most of the ways somebody would reach for it.

The things it has to get right are all things that were learned the hard way in this repo: the
overlay IS the release channel (a machine that has not synced cannot see a new build), the app and
the session are two packages that must move together, and /etc/sway/config belongs to Portage — an
etc-update replaces a hand-edited one, which is what silently reverted the key bindings during
development.
"""
import os
import re
import shutil
import stat
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CMD = ROOT / "os" / "bin" / "update-posterchan"
GENTOO = ROOT / "os" / "gentoo.sh"
EBUILD = ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild"


class TheCommandExists(unittest.TestCase):
    def test_it_is_there_and_executable(self):
        self.assertTrue(CMD.exists())
        self.assertTrue(os.stat(CMD).st_mode & stat.S_IXUSR, "not executable")

    @unittest.skipIf(shutil.which("bash") is None, "no bash")
    def test_it_parses(self):
        r = subprocess.run(["bash", "-n", str(CMD)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_both_installers_ship_it(self):
        """Installed by the script AND by the ebuild — a machine built either way must have it."""
        for f in (GENTOO, EBUILD):
            with self.subTest(file=f.name):
                self.assertIn("update-posterchan", f.read_text())


class ItUpdatesBothHalves(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = CMD.read_text()

    def test_it_updates_the_app_and_the_session(self):
        """Updating one alone leaves the desktop and the app that draws it out of step — a key
        binding sending a tick nothing listens for, or a helper the app cannot find."""
        self.assertIn("app-misc/posterchan-desktop", self.src)
        self.assertIn("app-misc/posterchanos-shell", self.src)

    def test_it_syncs_the_overlay_first(self):
        """The overlay is the release channel; without a sync there is nothing new to find."""
        self.assertIn("emaint sync -r posterchan", self.src)

    def test_it_does_not_sync_the_whole_tree(self):
        """A full `emerge --sync` is a large fetch with nothing to do with this app, and making
        people wait for it is how a one-command update becomes one nobody runs.

        Checked against the CODE, not the file: the comment above the sync explains what it is not
        doing, and naming the thing you avoid is not doing it. Matching prose is how these guards
        rot, and it has done so four times today."""
        code = "\n".join(l for l in self.src.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("emerge --sync", code)

    def test_an_unreachable_overlay_is_not_fatal(self):
        i = self.src.index("emaint sync")
        self.assertIn("Could not reach the overlay", self.src[i:i + 300])


class ItBehavesLikeACommandSomebodyTypes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = CMD.read_text()

    def test_it_asks_for_root_rather_than_refusing(self):
        """"Run this with sudo" is a worse answer than a password prompt."""
        self.assertRegex(self.src, r'exec sudo -- "\$0"')

    def test_it_says_when_nothing_changed(self):
        """"Updated" printed over a no-op is the same lie as a progress bar that finishes without
        doing anything."""
        self.assertIn("Already up to date", self.src)
        self.assertIn("before", self.src)

    def test_a_failed_update_says_the_desktop_is_untouched(self):
        i = self.src.index("The update failed")
        self.assertIn("untouched", self.src[i:i + 160])

    def test_it_does_not_restart_the_desktop_itself(self):
        """On PosterChanOS the shell IS the desktop: restarting it closes every window, and doing
        that inside an update somebody ran mid-task is how an update becomes something they avoid."""
        body = self.src
        self.assertNotRegex(body, r"(?m)^\s*(pkill|swaymsg exec)\s")
        self.assertIn("The new desktop starts at your next login", body)

    def test_it_warns_that_portage_owns_the_sway_config(self):
        self.assertIn("/etc/sway/config", self.src)
        self.assertIn("etc-update", self.src)


if __name__ == "__main__":
    unittest.main()
