"""Alt+Return opens PosterChan's terminal, and there is still a way in when the shell is broken.

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

Ported to `wayfire.ini`, where a binding is a `binding_x`/`command_x` pair and a command may be a
`;`-chain (Wayfire runs it through `sh -c`), which is how the recovery chord both suppresses the
Start menu and opens the terminal.
"""
import re
import unittest
from pathlib import Path

from tests.wayfire_config import bindings

ROOT = Path(__file__).resolve().parents[1]
OS_JS = ROOT / "static" / "js" / "client" / "os.js"


class ModReturnOpensOurs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binds = bindings()
        cls.cfg = "\n".join("%s = %s" % kv for kv in cls.binds.items())

    def test_it_is_bound(self):
        self.assertIn("<alt> KEY_ENTER", self.binds)

    def test_it_is_not_a_third_party_terminal(self):
        cmd = self.binds["<alt> KEY_ENTER"]
        self.assertNotIn("foot", cmd,
                         "Super+Return opens a different terminal emulator than the one this OS "
                         "ships, which is what was reported twice")
        for other in ("alacritty", "kitty", "xterm", "gnome-terminal", "konsole"):
            with self.subTest(term=other):
                self.assertNotIn(other, cmd)

    def test_it_reaches_the_shell(self):
        """Through the action socket, which is Wayfire's equivalent of Sway's send_tick: a binding
        can only run a command, so the command hands the payload to the running shell."""
        self.assertIn("pc-wayfire-action pc:terminal", self.binds["<alt> KEY_ENTER"])

    def test_the_shell_answers_it(self):
        """Through `openTerminalHere`, which ARMS THE LOCAL PTY and then opens the same window a
        click opens. The tick used to launch the app directly, so Alt+Return on the machine you are
        sitting at reattached to whatever host this device last SSH'd into — and `openTerminalHere`,
        which exists to prevent exactly that and says so in its own comment, had no callers at all.
        """
        src = OS_JS.read_text()
        self.assertIn("'pc:terminal'", src)
        i = src.index("else if(p === 'pc:terminal')")
        branch = src[i:src.index("else if(p === 'pc:tasks')", i)]
        self.assertIn("openTerminalHere()", branch,
                      "the tick is recognised but does not arm a shell on THIS machine")
        # And that helper is still the one that opens the ordinary window.
        helper = src[src.index("function openTerminalHere()"):]
        helper = helper[:helper.index("\n  }")]
        self.assertIn("PCTerm.openLocal", helper)
        self.assertIn("openApp('terminal')", helper,
                      "the terminal key must open the same window a click opens")

    def test_it_opens_it_the_same_way_a_click_does(self):
        """`openApp` is what a start-menu entry and a desktop icon go through, so the window the key
        opens is the same window, in the same place in the stacking order."""
        src = OS_JS.read_text()
        i = src.index("else if(p === 'pc:terminal')")
        self.assertNotIn("window.open", src[i:src.index("else if(p === 'pc:tasks')", i)])

    def test_its_super_release_cannot_open_start_over_the_terminal(self):
        """Old configs remain harmless for the first session before the package repairs them."""
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
        cls.binds = bindings()

    def test_foot_is_still_reachable(self):
        self.assertTrue([c for c, v in self.binds.items() if re.search(r"\bfoot\b", v)],
                        "foot was deleted rather than moved — with the desktop down there is now "
                        "no way to open a terminal at all")

    def test_it_does_not_go_through_the_shell(self):
        """Its whole value is owing the shell nothing: no tick, no IPC, no running desktop."""
        for chord, cmd in self.binds.items():
            if re.search(r"\bfoot\b", cmd):
                with self.subTest(chord=chord):
                    self.assertNotIn("pc-wayfire-action", cmd)
                    self.assertNotIn("swaymsg", cmd)

    def test_the_two_are_different_keys(self):
        foot = [c for c, v in self.binds.items() if re.search(r"\bfoot\b", v)]
        self.assertTrue(foot)
        self.assertNotIn("<alt> KEY_ENTER", foot)


if __name__ == "__main__":
    unittest.main()
