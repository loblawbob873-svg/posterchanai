"""The Electron bridge that lets the page act as a desktop shell.

`desktop/wm.js` and `desktop/net.js` were written and tested first and then sat there, called by
nothing — which is a shape worth naming, because tested code that is not wired up looks finished
from every angle except the one that matters. This is the wiring, and what it must not do is more
interesting than what it does: `launch` starts a PROCESS, `connect` hands a wifi password to
NetworkManager, and `provision` runs a command as ROOT.

Electron cannot be run here (it needs a display), so this reads the two files and asserts the
properties that would otherwise only be discovered by someone with a screen and bad luck.
"""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "desktop", "main.js")
PRELOAD = os.path.join(ROOT, "desktop", "preload.js")


class Bridge(unittest.TestCase):
    def setUp(self):
        self.main = open(MAIN, encoding="utf-8").read()
        self.pre = open(PRELOAD, encoding="utf-8").read()

    def test_both_files_parse(self):
        for f in (MAIN, PRELOAD):
            r = subprocess.run(["node", "--check", f], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr[-600:])

    def test_the_modules_are_actually_called(self):
        """The gap this test exists to close: two tested modules that nothing invoked."""
        self.assertIn("./wm.js", self.main)
        self.assertIn("./net.js", self.main)
        for surface in ("pcWM", "pcNet", "pcOS", "pcPower", "pcAudio"):
            self.assertIn(surface, self.pre, f"{surface} is not exposed to the page")

    def test_every_privileged_handler_checks_the_sender(self):
        """`launch` starts a process and `connect` hands over a wifi password. A handler reachable
        from any page but our own is a remote code execution, and the check is one call — which is
        exactly the kind of thing that gets left off one handler out of twelve."""
        missing = []
        for m in re.finditer(r"ipcMain\.handle\('(pc:(?:wm|net|os|power|audio):[a-z]+)'\s*,\s*(async\s*)?\("
                             r"[^)]*\)\s*=>\s*\{?([^\n]*)", self.main):
            name, body = m.group(1), m.group(3)
            tail = self.main[m.end():m.end() + 400]
            if "fsGuard" not in body and "fsGuard" not in tail:
                missing.append(name)
        self.assertEqual(missing, [], f"handlers that do not check the sender: {missing}")

    def test_launch_takes_an_argv_array_not_a_command_string(self):
        """A string would have to reach a shell to be useful, and then a file name with a space in
        it is an injection."""
        i = self.main.index("'pc:wm:launch'")
        body = self.main[i:i + 700]
        self.assertIn("Array.isArray", body, "a command string would be handed to a shell")
        self.assertNotIn("exec(", body)
        self.assertNotIn("shell: true", body)

    def test_provision_validates_the_npub_before_running_as_root(self):
        """It shells out to sudo. The page is not trusted to have checked its own input, and neither
        is the argument — the script checks it again on the other side."""
        i = self.main.index("'pc:os:provision'")
        body = self.main[i:i + 900]
        self.assertIn("npub1", body, "an unvalidated string is passed to a root command")
        self.assertIn("sudo", body)
        self.assertIn("-n", body, "sudo may not be allowed to prompt — it would hang the shell")

    def test_provision_runs_one_fixed_command(self):
        i = self.main.index("'pc:os:provision'")
        body = self.main[i:i + 900]
        self.assertIn("/usr/local/bin/pc-provision-user", body)
        self.assertIn("execFile", body, "a shell would make the argument executable")

    def test_a_launch_that_never_appears_is_not_reported_as_launched(self):
        i = self.main.index("'pc:wm:launch'")
        body = self.main[i:i + 900]
        self.assertIn("waitForWindow", body)

    def test_the_event_listener_can_be_removed(self):
        """The desktop redraws its taskbar on every window event; a listener the page cannot remove
        leaks a closure per view change."""
        i = self.pre.index("onEvent:")
        body = self.pre[i:i + 400]
        self.assertIn("removeListener", body)

    def test_ending_the_session_is_four_handlers_not_one_verb(self):
        """A single `pc:power:do(action)` is one typo away from a page asking to power off when it
        meant to sleep. Four names cannot be mistyped into each other."""
        for verb in ("suspend", "hibernate", "poweroff", "reboot"):
            self.assertIn(f"'pc:power:{verb}'", self.main, f"{verb} has no handler of its own")
        # The HANDLER, not the phrase — the comment above it explains why a verb argument would be
        # wrong, and a test that matches prose is a test about the comments.
        self.assertNotIn("ipcMain.handle('pc:power:do'", self.main)

    def test_the_modules_are_required_not_reimplemented(self):
        for mod in ("./power.js", "./audio.js"):
            self.assertIn(mod, self.main, f"{mod} is tested and not called")

    def test_it_is_absent_rather_than_broken_without_a_compositor(self):
        """A desktop install that is not PosterChanOS has no sway. The page must be able to ask,
        rather than discovering it through a thrown error on every call."""
        self.assertIn("pc:wm:available", self.main)
        self.assertIn("available:", self.pre)


if __name__ == "__main__":
    unittest.main()
