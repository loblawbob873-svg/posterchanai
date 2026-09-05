"""THE COMPOSITOR'S WINDOW FRAME IS PART OF THE PRODUCT AND USES THE PRODUCT'S COLOURS.

Reported as "why the fuck are the windows purple? that does not match posterchan at all". The frame
was `#241438` / `#171222` -- a purple that appears nowhere in the client. The client's palette is
declared once at the top of client.css: `--bg` is the page and `--bg2` the raised surface every
panel and title bar in the app is drawn on, and a compositor frame is that same kind of surface.

This test reads BOTH files and compares them, so the two cannot drift again: changing the palette
without changing the frame fails here, which is the only way a test can hold a colour that lives in
a compositor config and a stylesheet at once.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
INI = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def _token(name):
    """A colour from the client's :root palette."""
    root = CSS[CSS.index(":root{"):]
    root = root[: root.index("}")]
    m = re.search(re.escape("--" + name) + r"\s*:\s*(#[0-9a-fA-F]{3,8})", root)
    assert m, f"--{name} is not a plain hex colour in :root any more"
    return m.group(1).lower()


def _frame(option):
    m = re.search(re.escape(option) + r"\s*=\s*\\?(#[0-9a-fA-F]{6,8})", INI)
    assert m, f"{option} is not set in wayfire.ini"
    return m.group(1).lower()


class TestTheFrameUsesThePalette(unittest.TestCase):
    def test_the_focused_frame_is_the_raised_surface(self):
        self.assertEqual(_frame("active_color"), _token("bg2") + "ff",
                         "the focused window frame is not the client's raised-surface colour")

    def test_the_unfocused_frame_is_the_page(self):
        self.assertEqual(_frame("inactive_color"), _token("bg") + "ff",
                         "the unfocused window frame is not the client's page colour")

    def test_the_old_purple_is_gone(self):
        for dead in ("#241438", "#171222"):
            self.assertNotIn(dead, INI.lower(),
                             f"{dead} is a colour that appears nowhere in the client")

    def test_the_frame_colours_are_opaque(self):
        """A translucent frame samples whatever is behind it, so it matches nothing reliably."""
        for option in ("active_color", "inactive_color"):
            self.assertTrue(_frame(option).endswith("ff"), option)


class TestTheReasonItIsNotThemedIsRecorded(unittest.TestCase):
    def test_the_config_says_why_it_cannot_follow_the_user_theme(self):
        """There are seven themes switched at runtime and this is a static compositor file; the one
        way to change it live is the rewrite that killed a session. Someone will ask why -- the
        answer belongs beside the values, not in a commit message nobody will find."""
        block = INI[INI.index("[decoration]"): INI.index("[move]")]
        self.assertIn("theme", block.lower())


if __name__ == "__main__":
    unittest.main()
