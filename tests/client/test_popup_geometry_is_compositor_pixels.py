"""A POPUP WINDOW'S GEOMETRY IS COMPOSITOR PIXELS; EVERY SIZE IN os.js IS LAYOUT PIXELS.

`vwL`/`vhL` divide `innerWidth`/`innerHeight` by `zf()`, because that is what the stylesheet's
100vw/100vh mean inside a `body{zoom}` -- so a menu capped at `min(780px, 100vw-20px)` is 780
LAYOUT px. But `getBoundingClientRect()` IS scaled by that zoom, and `pcPopup.open`/`toggle` pass
their numbers straight to the compositor (main.js `placePopupWindow` -> `placeAndReveal`).

Every opener mixed the two in a single expression. `r.top - h - 8` subtracts a layout height from a
scaled coordinate, and the window was asked for at 1/zf of the size its own contents render at.

At zoom 1 the spaces are identical, so this is invisible until a display scale is in force. On a
3840x2560 panel at 1.25 the start menu is asked for at 920px while its contents render 1150, and its
top edge is computed 230px too low -- reported as "start menu is not attached to the taskbar, looks
weird". The notification centre, the network panel and the tray flyout had the same arithmetic.

This is the `openPop` mistake the codebase already recorded once (rect vs offsetWidth vs style.left)
in five more places, which is why the fix is one named conversion at the boundary rather than five
corrections.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
SHELL_JS = (ROOT / "static/js/client/osshell.js").read_text(encoding="utf-8")

# Every call that hands a rectangle to the compositor.
OPENERS = re.compile(r"pcPopup\.(?:open|toggle)\(\s*'(\w+)'\s*,\s*\{([^}]*)\}")


class TestEveryOpenerConverts(unittest.TestCase):
    def test_there_is_one_named_conversion(self):
        self.assertIn("const popupPx =", OS_JS,
                      "no single place converts layout pixels to compositor pixels")

    def test_no_opener_passes_a_layout_size_to_the_compositor(self):
        found = OPENERS.findall(OS_JS)
        self.assertGreaterEqual(len(found), 4, f"expected the four os.js popups, found {found}")
        for kind, args in found:
            for field in ("width", "height"):
                value = re.search(field + r"\s*:\s*([A-Za-z0-9_]+)", args)
                self.assertIsNotNone(value, f"{kind}: no {field} in {args!r}")
                self.assertIn(value.group(1), ("wD", "hD"),
                              f"{kind} passes {value.group(1)!r} as {field} — that is a layout size")

    def test_the_anchored_popups_subtract_a_converted_height(self):
        """`r.top - h - 8` is the actual defect: two coordinate spaces in one subtraction."""
        for anchor in ("#os-bell", "#os-net"):
            block = OS_JS[OS_JS.index(anchor) - 400: OS_JS.index(anchor) + 400]
            self.assertNotRegex(block, r"r\.top\s*-\s*h\s*-",
                                f"the popup anchored to {anchor} still mixes the two spaces")
            self.assertNotRegex(block, r"r\.right\s*-\s*w\b",
                                f"the popup anchored to {anchor} still mixes the two spaces")

    def test_the_start_menu_falls_back_in_the_same_space_it_measures_in(self):
        """Its fallback was `vhL()` — layout — subtracted from by a value in the other space."""
        body = OS_JS[OS_JS.index("function _startPopup()"):]
        body = body[: body.index("function toggleStart")]
        self.assertNotIn("rect.top : vhL()", body)
        self.assertIn("window.innerHeight", body)

    def test_the_composer_places_itself_in_compositor_pixels(self):
        body = OS_JS[OS_JS.index("function _composeInWindow"):]
        body = body[: body.index("function renderComposePopup")]
        self.assertNotIn("(vwL() - w) / 2", body)
        self.assertIn("window.innerWidth", body)


class TestTheTrayFlyoutToo(unittest.TestCase):
    def test_it_scales_its_css_width(self):
        body = SHELL_JS[SHELL_JS.index("function openTrayWindow"):]
        body = body[: body.index("\n  }")]
        self.assertNotRegex(body, r"const w = 360;",
                            "the tray flyout still asks for its CSS width as a compositor width")
        self.assertIn("offsetWidth", body,
                      "nothing measures the zoom, so the conversion cannot be right")


class TestTheCommentSurvivesItself(unittest.TestCase):
    def test_os_js_parses(self):
        """A `*/` inside the explanation would end the comment and take the file with it — which
        happened while writing this fix, and is the same shape as the backtick that once blanked
        every icon in the client."""
        import subprocess
        out = subprocess.run(["node", "--check", str(ROOT / "static/js/client/os.js")],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main()
