"""Super+Return opens PosterChan's terminal, and there is still a way in when the shell is broken.

    "terminal is also not the one I wanted you to build"
    "win + enter not loading PosterChan terminal"

It ran `foot` — a different terminal emulator that happens to be installed. So the keystroke every
Linux user has in their fingers opened the wrong program, and the terminal this OS actually ships
went unused. Not broken, exactly: pointed at the wrong thing, which is harder to notice because
something does open.

The second half matters as much. This is an operating system, and the tick reaches nothing when the
desktop is not running — which is precisely the moment somebody needs a terminal to find out why.
`foot` is a separate process that owes the shell nothing, so it keeps a key. MOVING it rather than
deleting it is the difference between changing a default and removing an escape hatch.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SWAY = ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config"
OS_JS = ROOT / "static" / "js" / "client" / "os.js"


class ModReturnOpensOurs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = SWAY.read_text()
        cls.binds = dict(re.findall(r"(?m)^bindsym\s+(\S+)\s+exec\s+(.+)$", cls.cfg))

    def test_it_is_bound(self):
        self.assertIn("$mod+Return", self.binds)

    def test_it_is_not_a_third_party_terminal(self):
        cmd = self.binds["$mod+Return"]
        self.assertNotIn("foot", cmd,
                         "Super+Return opens a different terminal emulator than the one this OS "
                         "ships, which is what was reported twice")
        for other in ("alacritty", "kitty", "xterm", "gnome-terminal", "konsole"):
            with self.subTest(term=other):
                self.assertNotIn(other, cmd)

    def test_it_reaches_the_shell(self):
        self.assertIn("send_tick pc:terminal", self.binds["$mod+Return"])

    def test_the_shell_answers_it(self):
        src = OS_JS.read_text()
        self.assertIn("'pc:terminal'", src)
        i = src.index("'pc:terminal'")
        self.assertIn("openApp('terminal')", src[i:i + 160],
                      "the tick is recognised but does not open the terminal view")

    def test_it_opens_it_the_same_way_a_click_does(self):
        """`openApp` is what a start-menu entry and a desktop icon go through, so the window the key
        opens is the same window, in the same place in the stacking order."""
        src = OS_JS.read_text()
        i = src.index("'pc:terminal'")
        self.assertNotIn("window.open", src[i:i + 160])

    def test_its_super_release_cannot_open_start_over_the_terminal(self):
        """Sway emits the bare-Super release after the Return binding has already fired."""
        src = OS_JS.read_text()
        terminal = src.index("else if(p === 'pc:terminal')")
        start = src.index("if(p === 'pc:start')")
        self.assertIn("_suppressStartUntil =", src[terminal:terminal + 260])
        self.assertIn("Date.now() < _suppressStartUntil", src[start:start + 260])
        self.assertIn("toggleStart(false)", src[terminal:terminal + 260])

    def test_ctrl_enter_does_not_steal_send_from_an_editor(self):
        """The OS handler runs in capture, before a composer can consume Ctrl+Enter itself."""
        src = OS_JS.read_text()
        self.assertNotRegex(src, r"if\s*\([^)]*e\.ctrlKey[^)]*(Enter|NumpadEnter)",
                            "Ctrl+Enter still opens Terminal instead of sending the message")


class TheWayBackSurvives(unittest.TestCase):
    """A shell that will not start is the case a bare terminal exists for."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = SWAY.read_text()

    def test_foot_is_still_reachable(self):
        self.assertRegex(self.cfg, r"(?m)^bindsym\s+\S+\s+exec\s+foot\b",
                         "foot was deleted rather than moved — with the desktop down there is now "
                         "no way to open a terminal at all")

    def test_it_does_not_go_through_the_shell(self):
        """Its whole value is owing the shell nothing."""
        for key, cmd in re.findall(r"(?m)^bindsym\s+(\S+)\s+exec\s+(.+)$", self.cfg):
            if cmd.strip().startswith("foot"):
                with self.subTest(key=key):
                    self.assertNotIn("swaymsg", cmd)

    def test_the_two_are_different_keys(self):
        binds = dict(re.findall(r"(?m)^bindsym\s+(\S+)\s+exec\s+(.+)$", self.cfg))
        foot = [k for k, v in binds.items() if v.strip().startswith("foot")]
        self.assertTrue(foot)
        self.assertNotIn("$mod+Return", foot)


if __name__ == "__main__":
    unittest.main()
