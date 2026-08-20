"""The Print key takes a screenshot, through the same code the tray button uses.

    "screenshots still do nothing"

Everything was in place except the one thing anybody uses: grim and slurp are installed, the tray has
had a Screenshot button since the shell did, `pcShot` is on the preload bridge and main.js handles it
— and there was no Print binding at all. So the feature worked and the key did nothing, which is
indistinguishable from the feature being broken.

It is bound to a TICK rather than to a grim command line, for the same reason the Super key is: a
sway binding can only run a command, and running grim from the config would be a second
implementation of something the shell already does — its own directory, its own filename, its own
clipboard copy, its own "saved to…" notice. Two of those drift, and the one nobody is watching is the
one that rots.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWAY = ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config"
OS_JS = ROOT / "static" / "js" / "client" / "os.js"
SHELL = ROOT / "static" / "js" / "client" / "osshell.js"


class ThePrintKeyIsBound(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = SWAY.read_text()

    def test_print_is_bound(self):
        self.assertRegex(self.cfg, r"(?m)^bindsym\s+Print\s+exec\b",
                         "no Print binding — the key every keyboard has for this does nothing")

    def test_a_region_shot_has_its_own_binding(self):
        self.assertRegex(self.cfg, r"(?m)^bindsym\s+Shift\+Print\s+exec\b")

    def test_neither_shells_out_to_grim_directly(self):
        """A second implementation is how the key and the button end up disagreeing about where a
        screenshot goes."""
        for line in self.cfg.splitlines():
            if re.match(r"^bindsym\s+(Shift\+)?Print\b", line):
                with self.subTest(line=line.strip()):
                    self.assertIn("send_tick", line)
                    self.assertNotIn("grim", line)
                    self.assertNotIn("slurp", line)

    def test_the_two_ticks_differ(self):
        ticks = re.findall(r"(?m)^bindsym\s+(?:Shift\+)?Print\s+exec\s+swaymsg -t send_tick (\S+)",
                           self.cfg)
        self.assertEqual(len(ticks), 2)
        self.assertEqual(len(set(ticks)), 2, "both Print bindings send the same tick")


class TheShellAnswersThem(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = OS_JS.read_text()

    def test_every_tick_the_config_sends_is_handled(self):
        """A binding whose tick nothing listens for is a key that does nothing — which is the bug."""
        for tick in re.findall(r"send_tick (pc:[a-z:]+)", SWAY.read_text()):
            with self.subTest(tick=tick):
                self.assertIn("'%s'" % tick, self.src,
                              "sway sends %s and os.js does not handle it" % tick)

    def test_it_goes_through_the_tray_buttons_function(self):
        self.assertIn("PCOSShell.takeShot", self.src)
        self.assertIn("takeShot", SHELL.read_text())

    def test_takeshot_is_actually_exported(self):
        """It is called across module boundaries, which is exactly where this codebase has produced
        phantom functions before."""
        shell = SHELL.read_text()
        i = shell.index("root.PCOSShell = API")
        api = shell[max(0, i - 1200):i]
        self.assertRegex(api, r"\btakeShot\b",
                         "takeShot is not on the PCOSShell surface, so os.js calls undefined")

    def test_the_call_is_guarded(self):
        """The desktop can be entered where PCOSShell exists with nothing behind it."""
        i = self.src.index("function _shot(")
        self.assertIn("window.PCOSShell", self.src[i:i + 400])
        self.assertIn("catch", self.src[i:i + 400])


if __name__ == "__main__":
    unittest.main()
