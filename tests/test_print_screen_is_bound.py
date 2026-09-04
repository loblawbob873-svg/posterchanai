"""The Print key takes a screenshot, through the same code the tray button uses.

    "screenshots still do nothing"

Everything was in place except the one thing anybody uses: grim and slurp are installed, the tray has
had a Screenshot button since the shell did, `pcShot` is on the preload bridge and main.js handles it
— and there was no Print binding at all. So the feature worked and the key did nothing, which is
indistinguishable from the feature being broken.

It runs the packaged HELPER rather than a grim command line, for the same reason the Super key does:
a compositor binding can only run a command, and running grim from the config would be a second
implementation of something that already has its own directory, filename, clipboard copy and
"saved to…" notice. Two of those drift, and the one nobody is watching is the one that rots.

Ported from the Sway config to `wayfire.ini`, where a binding is a `binding_x`/`command_x` PAIR —
and where `Ctrl+Shift+S` had been silently dropped in the move.
"""
import unittest
from pathlib import Path

from tests.wayfire_config import bindings, runs

ROOT = Path(__file__).resolve().parents[1]
OS_JS = ROOT / "static" / "js" / "client" / "os.js"
SHELL = ROOT / "static" / "js" / "client" / "osshell.js"


class ThePrintKeyIsBound(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binds = bindings()

    def test_print_is_bound(self):
        self.assertIn("KEY_SYSRQ", self.binds,
                      "no Print binding — the key every keyboard has for this does nothing")

    def test_a_whole_screen_shot_has_its_own_binding(self):
        self.assertIn("<shift> KEY_SYSRQ", self.binds)

    def test_ctrl_shift_s_is_bound(self):
        """The chord people arriving from any other desktop reach for. It existed on the Sway
        session and did not survive the move; nothing failed, the key simply stopped working."""
        self.assertIn("<ctrl> <shift> KEY_S", self.binds)
        self.assertIn("pc-screenshot region", self.binds["<ctrl> <shift> KEY_S"])

    def test_bindings_use_the_packaged_helper(self):
        for chord in ("KEY_SYSRQ", "<shift> KEY_SYSRQ", "<ctrl> <shift> KEY_S"):
            with self.subTest(chord=chord):
                self.assertIn("/usr/local/bin/pc-screenshot", self.binds[chord])

    def test_the_two_modes_differ(self):
        """Region and whole-screen are different actions; binding both to one is a silent loss."""
        self.assertTrue(runs("pc-screenshot region"))
        self.assertTrue(runs("pc-screenshot screen"))
        self.assertNotEqual(set(runs("pc-screenshot region")), set(runs("pc-screenshot screen")))


class TheShellAnswersTheTicksTheConfigSends(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = OS_JS.read_text()

    def test_every_tick_the_config_sends_is_handled(self):
        """A binding whose tick nothing listens for is a key that does nothing — which is the bug."""
        shell = SHELL.read_text()
        for chord, command in bindings().items():
            if "pc-wayfire-action" not in command:
                continue
            for word in command.split():
                if word.startswith("pc:"):
                    with self.subTest(tick=word):
                        self.assertTrue(word in self.src or word in shell,
                                        f"{chord} sends {word} and nothing listens for it")
