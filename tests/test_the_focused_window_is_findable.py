"""The focused window must be findable at a glance — on NATIVE apps too, not just ours.

Reported as "where the fuck is the hyprland color around the entire windows!".

Two layers draw a focused border and only one of them was doing anything:

  * PosterChan's OWN windows are `.osw` elements and take the gradient ring in client.css.
  * EVERY OTHER window — Firefox, Telegram, a terminal — is a compositor window. No client CSS can
    reach it; `[decoration]` in wayfire.ini is the only thing that can, and `ignore_views` there
    deliberately excludes ours so the two never double up.

Wayfire's decoration paints a title bar and its border band in ONE colour, so no single value was
both a quiet title bar and a findable border. The accent is now a ring drawn by the posterchan-shell
plugin around every application window; [decoration] keeps the title bar on the raised surface.
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

    def test_the_focused_frame_is_findable_without_being_a_cyan_title_bar(self):
        """THREE REPORTS, AND WAYFIRE COULD ONLY EVER ANSWER TWO OF THEM AT ONCE.

        `active_color` fills a decorated window's title bar AND its border band with one colour:
        `--bg2` was an invisible border ("where the fuck is the hyprland color around the entire
        windows"), `--neon` a cyan title bar ("firefox is now a bright cyan window title?"), and the
        blend between them a teal box matching nothing ("window color has a different color from the
        rest of the desktop and no border").

        So the jobs are split. [decoration] paints the title bar the raised surface, and the accent
        is a ring the posterchan-shell plugin draws over the border band -- and around Firefox, which
        Wayfire never decorates at all."""
        self.assertTrue(_colour("active_color").startswith(_token("bg2")),
                        "the focused title bar is not the raised surface")
        shell = INI.split("[posterchan-shell]", 1)[1].split("\n[", 1)[0]
        self.assertRegex(shell, r"(?m)^window_border\s*=\s*true\s*$",
                         "nothing draws the accent ring, so a focused window is findable by nothing")

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
        """A native app and one of ours must agree about what "focused" looks like: the ring is the
        client's own accent (tests/test_native_windows_match_the_desktop.py holds the exact value)."""
        ring = CSS[CSS.index(".os-root.os-fx .osw.focused:not(.osw-document)::after{"):]
        ring = ring[:ring.index("}")]
        self.assertIn("var(--neon)", ring)
        shell = INI.split("[posterchan-shell]", 1)[1].split("\n[", 1)[0]
        m = re.search(r"(?m)^window_border_active_color\s*=\s*\\?#([0-9a-fA-F]{6})", shell)
        self.assertIsNotNone(m, "the ring has no focused colour")
        r, g, b = (int(m.group(1)[i:i + 2], 16) for i in (0, 2, 4))
        self.assertGreater(min(g, b), 150, "the focused ring is not a bright accent")


if __name__ == "__main__":
    unittest.main()
