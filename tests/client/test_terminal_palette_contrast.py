"""The terminal's colours are READABLE on the terminal's own background.

A shell prints in the sixteen ANSI colours whatever the screen looks like — `ls` puts directories in
blue, prompts are green and cyan, and plenty of tools print in "black" on the assumption that the
terminal will pick a black that shows. xterm's defaults did not know PosterChan's palette, and the
old theme set only background/foreground/cursor, leaving xterm's stock ANSI table (whose `black`
IS black, 1.0:1 on this background) to draw against a violet-black ground.

`TERM_THEME` is lifted out of the SHIPPED term.js and evaluated with node (it is a JS object
literal, not JSON), then every colour is measured with the WCAG 2.x contrast formula:

  * the default foreground  >= 4.5:1  (body text)
  * each of the 16 ANSI colours >= 3:1  (the large/UI-text floor — they are accents, not prose)
  * the cursor >= 3:1, so the one thing that says where you are typing can be found
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TERM = os.path.join(ROOT, "static", "js", "client", "term.js")
ANSI = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
        "brightBlack", "brightRed", "brightGreen", "brightYellow",
        "brightBlue", "brightMagenta", "brightCyan", "brightWhite"]


def _theme(path=TERM):
    src = open(path, encoding="utf-8").read()
    m = re.search(r"const TERM_THEME = (\{.*?\n\s*\});", src, re.S)
    if not m:
        return None
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node is not installed")
    out = subprocess.run([node, "-e", "process.stdout.write(JSON.stringify(" + m.group(1) + "))"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _lum(hexc):
    h = hexc.lstrip("#")
    assert re.fullmatch(r"[0-9a-fA-F]{6}", h), f"{hexc!r} is not a #rrggbb colour"
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a, b):
    x, y = sorted((_lum(a), _lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


class TerminalPaletteContrast(unittest.TestCase):
    def setUp(self):
        self.t = _theme()
        self.assertIsNotNone(self.t, "term.js has no TERM_THEME — the terminal uses xterm's stock "
                                     "ANSI table, whose black is invisible on this background")

    def test_all_sixteen_ansi_colours_are_defined(self):
        missing = [k for k in ANSI if k not in self.t]
        self.assertEqual(missing, [], "these ANSI colours fall back to xterm's defaults")

    def test_the_default_foreground_is_body_text_readable(self):
        c = contrast(self.t["foreground"], self.t["background"])
        self.assertGreaterEqual(c, 4.5, f"foreground {self.t['foreground']} is {c:.2f}:1")

    def test_every_ansi_colour_is_readable_on_the_background(self):
        bad = {k: round(contrast(self.t[k], self.t["background"]), 2) for k in ANSI
               if k in self.t and contrast(self.t[k], self.t["background"]) < 3.0}
        self.assertEqual(bad, {}, "ANSI colours under 3:1 against the terminal background")

    def test_the_cursor_can_be_found(self):
        self.assertGreaterEqual(contrast(self.t["cursor"], self.t["background"]), 3.0)

    def test_the_theme_is_what_xterm_is_given(self):
        """A palette that is defined and never passed to xterm measures perfectly and changes
        nothing on screen."""
        src = open(TERM, encoding="utf-8").read()
        mount = src[src.index("function _mountTerm()"):src.index("let _fitT = null")]
        self.assertIn("theme: _termTheme()", mount)
        fn = src[src.index("function _termTheme()"):src.index("function _termTheme()") + 1200]
        self.assertIn("Object.assign({}, TERM_THEME)", fn)
        # The theme's own neon only replaces the cursor when it is visible on this ground.
        self.assertIn(">= 4.5", fn)


if __name__ == "__main__":
    unittest.main()
