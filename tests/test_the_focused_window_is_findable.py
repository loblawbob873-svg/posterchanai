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

    def test_the_focused_frame_is_findable_without_being_the_accent_itself(self):
        """TWO REPORTS, ONE LINE, AND THE ANSWER IS BETWEEN THEM.

        First: `--bg2` is a panel SURFACE, and as a frame against a `--bg` desktop it is a dark grey
        line on a nearly-black one — "where the fuck is the hyprland color around the entire
        windows". So this test used to demand the raw accent.

        Then it got the raw accent, and: "firefox is now a bright cyan window title?". Wayfire's
        `active_color` is not a border colour — it fills the whole TITLEBAR — so `--neon` does not
        outline a window, it repaints its chrome in full cyan.

        The frame must therefore be DERIVED from the accent, not equal to it: `--frame-focus`, which
        is `--neon` at ~30% over `--bg2`. Still one palette (the token lives in client.css, so a
        hand-typed hex cannot drift back in), still obviously the focused window, not a headline."""
        active = _colour("active_color")
        self.assertTrue(active.startswith(_token("frame-focus")),
                        "focused frame is %s, not the frame token %s" % (active, _token("frame-focus")))
        self.assertFalse(active.startswith(_token("bg2")),
                         "focused frame is the panel surface again — invisible against the desktop")
        self.assertFalse(active.startswith(_token("neon")),
                         "focused frame is the raw accent again — that is a cyan titlebar, not a border")

    def test_the_frame_colour_is_still_a_client_token(self):
        """The rule the tone-down must not cost: the compositor and the client keep ONE palette, so
        the frame cannot be a hex somebody typed into wayfire.ini."""
        css = (ROOT / "static/css/client.css").read_text()
        self.assertIn("--frame-focus:", css,
                      "the frame colour is no longer defined in the client palette")

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
        # The RING is a thin gradient outline drawn by the client, so it keeps the full accent; the
        # compositor FRAME is a filled titlebar and takes the derived tone. Same family, different
        # jobs — which is why this test checks they agree in ORIGIN, not in exact value.
        self.assertIn("var(--neon)", ring)
        # They agree in ORIGIN, not in exact value: `--frame-focus` IS `--neon` blended towards the
        # surface, so both layers are the same accent doing two different jobs. Demanding the same
        # literal is what put a cyan titlebar on every window.
        self.assertTrue(_colour("active_color").startswith(_token("frame-focus")))
        self.assertNotEqual(_token("frame-focus"), _token("bg2"),
                            "the frame token collapsed back onto the panel surface")
        self.assertNotEqual(_token("frame-focus"), _token("neon"),
                            "the frame token is just the accent — that is the cyan titlebar again")


if __name__ == "__main__":
    unittest.main()
