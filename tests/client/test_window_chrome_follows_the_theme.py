"""WINDOW CHROME COMES FROM THE PALETTE, NOT FROM A COLOUR SOMEBODY TYPED.

Reported as "why are the windows purple? they don't fit in at all". The title bar drawn inside a
popped-out window was

    #pc-oswin-chrome{ background:linear-gradient(180deg,rgba(38,32,56,.98),rgba(20,17,31,.98)) }

`rgba(38,32,56)` is #262038 -- hardcoded rather than taken from the palette, so it stayed purple
through every one of the seven themes and matched nothing else on screen.

IT SURVIVED A FIRST FIX, which is the part worth keeping. That fix corrected the COMPOSITOR's frame
(`wayfire.ini`'s active_color, also a purple nobody chose). Measured on the real desk afterwards:
the compositor frame samples (11,11,16) -- correct -- with the client's own bar still #262038
directly beside it. Two surfaces, one complaint, and fixing the one that was easier to find made no
visible difference.

A title bar is the same KIND of surface as an in-page window's `.osw-bar`, which uses `--panel2`.
So they use the same token and cannot drift.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def _block(selector, needs="background"):
    """The rule for `selector` that actually sets `needs`.

    A selector can appear several times -- `.osw-bar` has a `user-select` rule above the one that
    paints it -- and taking the first match reported "no colour at all", which is the one answer
    that must not read as a pass here.
    """
    at = 0
    while True:
        i = CSS.index(selector + "{", at)
        block = CSS[i: CSS.index("}", i)]
        if needs in block:
            return block
        at = i + 1


def _decls(block):
    """Declarations only -- comments in this stylesheet quote the colours they replaced."""
    return re.sub(r"/\*.*?\*/", "", block, flags=re.S)


class TestThePoppedOutTitleBar(unittest.TestCase):
    def test_it_takes_its_background_from_the_palette(self):
        body = _decls(_block("#pc-oswin-chrome"))
        m = re.search(r"background:([^;]+);", body)
        self.assertIsNotNone(m, body)
        self.assertIn("var(--", m.group(1),
                      f"the window title bar hardcodes a colour: {m.group(1).strip()}")

    def test_it_matches_the_docked_window_title_bar(self):
        """A window that pops out must not change colour on the way."""
        # The token, however it is wrapped -- the docked bar reads
        # `color-mix(in srgb, var(--panel2) 91%, transparent)` because it has the page behind it,
        # while a popped-out window is opaque. Same token, different transparency, and it is the
        # TOKEN that has to match.
        def token(block):
            m = re.search(r"background:[^;]*?var\((--[a-z0-9-]+)\)", _decls(block))
            return m.group(1) if m else None
        popped = token(_block("#pc-oswin-chrome"))
        docked = token(_block(".osw-bar"))
        self.assertIsNotNone(popped, "the popped-out title bar has no palette token")
        self.assertIsNotNone(docked, "the docked title bar has no palette token")
        self.assertEqual(popped, docked,
                         "a popped-out window and a docked one wear different title bars")

    def test_the_purple_is_gone(self):
        self.assertNotIn("rgba(38,32,56", _decls(CSS))


class TestNoWindowChromeHardcodesAColour(unittest.TestCase):
    """The rule, not the one instance. A hardcoded colour in chrome is invisible in the theme it was
    written for and wrong in the other six."""

    CHROME = ["#pc-oswin-chrome", ".osw-bar", ".osw"]

    def test_every_chrome_surface_uses_a_token(self):
        bad = []
        for sel in self.CHROME:
            body = _decls(_block(sel))
            for prop in ("background", "background-color", "border"):
                for m in re.finditer(prop + r":([^;]+);", body):
                    value = m.group(1)
                    if re.search(r"#[0-9a-fA-F]{3,8}\b", value) or "rgba(" in value:
                        if "var(--" not in value and "transparent" not in value:
                            bad.append(f"{sel} {prop}:{value.strip()}")
        self.assertEqual(bad, [], "window chrome hardcodes colours: " + "; ".join(bad))


class TestTheCompositorFrameToo(unittest.TestCase):
    def test_the_other_half_of_the_same_complaint_is_still_fixed(self):
        """Both surfaces, one complaint. Fixing one and shipping is what happened the first time."""
        ini = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text(
            encoding="utf-8")
        self.assertNotIn("241438", ini)


if __name__ == "__main__":
    unittest.main()
