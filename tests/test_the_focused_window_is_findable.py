"""The focused window must be findable at a glance — on NATIVE apps too, not just ours.

Reported as "where the fuck is the hyprland color around the entire windows!".

Two layers draw a focused border and only one of them was doing anything:

  * PosterChan's OWN windows are `.osw` elements and take the gradient ring in client.css.
  * EVERY OTHER window — Firefox, Telegram, a terminal — is a compositor window. No client CSS can
    reach it; `[decoration]` in wayfire.ini is the only thing that can, and `ignore_views` there
    deliberately excludes ours so the two never double up.

The compositor's focused colour was `--bg2` (#12121a), which is the token a raised PANEL is drawn
on. Three pixels of it against a `--bg` (#0a0a0f) desktop is a dark grey line, i.e. no visible
border at all.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INI = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def _decoration():
    body = INI.split("[decoration]", 1)[1]
    return body.split("\n[", 1)[0]


def _token(name):
    m = re.search(r"--" + name + r":(#[0-9a-fA-F]{3,8})", CSS)
    assert m, "no --%s in client.css" % name
    return m.group(1).lower()


def _colour(key):
    m = re.search(r"^%s\s*=\s*\\?#([0-9a-fA-F]{6,8})" % key, _decoration(), re.M)
    assert m, "no %s in [decoration]" % key
    return ("#" + m.group(1)).lower()


class TheFocusedBorderIsVisible(unittest.TestCase):

    def test_the_focused_frame_is_the_accent_not_a_surface(self):
        """THE BUG: `--bg2` is a panel surface. As a border it is invisible against the desktop."""
        active = _colour("active_color")
        self.assertTrue(active.startswith(_token("neon")),
                        "focused border is %s, not the accent %s" % (active, _token("neon")))
        self.assertFalse(active.startswith(_token("bg2")), "focused border is the panel surface again")

    def test_an_unfocused_window_recedes(self):
        """Only one window is focused; if every frame is bright, none of them reads as focused."""
        self.assertTrue(_colour("inactive_color").startswith(_token("bg")))

    def test_the_border_is_thick_enough_to_see(self):
        m = re.search(r"^border_size\s*=\s*(\d+)", _decoration(), re.M)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 2)

    def test_our_own_windows_are_left_to_the_client(self):
        """`ignore_views` keeps the compositor off PosterChan windows so the CSS gradient ring is
        the only border there — two borders on one window is worse than none."""
        self.assertIn("ignore_views", _decoration())
        self.assertIn("posterchan-desktop", _decoration())

    def test_both_layers_use_the_same_accent(self):
        """A native app and one of ours must agree about what "focused" looks like."""
        ring = CSS[CSS.index(".os-root.os-fx .osw.focused:not(.osw-document)::after{"):]
        ring = ring[:ring.index("}")]
        self.assertIn("var(--neon)", ring)
        self.assertTrue(_colour("active_color").startswith(_token("neon")))


if __name__ == "__main__":
    unittest.main()
