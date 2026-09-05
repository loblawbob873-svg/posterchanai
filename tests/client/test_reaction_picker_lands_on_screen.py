"""RECT PIXELS IN, STYLE PIXELS OUT -- AND THEY ARE NOT THE SAME PIXEL UNDER `body{zoom}`.

`reactionPickerPosition` works in `getBoundingClientRect()` space, which IS scaled by the page
zoom. `placeReactionPicker` wrote that straight into `style.left`/`style.top` on a `position:fixed`
child of the zoomed body -- and those are NOT scaled; they are multiplied by the zoom on the way to
the screen.

So at a 1.25 display scale the picker was placed 25% further right and further down than it was
told, which for a control anchored near the right edge of a message row puts it off the screen.
Reported as "emoji reaction in concord does nothing": the button worked, the picker opened, and
nobody could see it.

The same mistake as the taskbar flyouts, and as `openPop` on the desktop before them -- which is why
this test measures the CONVERSION rather than a particular number.
"""
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def place(zoom, anchor_css_width=24):
    """Run the shipped placer against a stubbed anchor at a given page zoom."""
    src = CONCORD[CONCORD.index("  function reactionPickerPosition("):]
    src = src[: src.index("  function normalizeIcon(")]
    tmp = Path(tempfile.mkdtemp())
    try:
        js = tmp / "t.js"
        js.write_text(f"""
const ZOOM = {zoom}, CSSW = {anchor_css_width};
// An anchor 24 css px wide, sitting at css x=1000,y=900 -- its RECT is those times the zoom.
const anchor = {{
  offsetWidth: CSSW,
  getBoundingClientRect: () => ({{ left: 1000*ZOOM, right: (1000+CSSW)*ZOOM,
                                  top: 900*ZOOM, bottom: (900+24)*ZOOM,
                                  width: CSSW*ZOOM, height: 24*ZOOM }}),
}};
const style = {{}};
const picker = {{
  offsetWidth: 240, offsetHeight: 44,
  getBoundingClientRect: () => ({{ width: 240*ZOOM, height: 44*ZOOM }}),
  style: {{ setProperty: (k, v) => {{ style[k] = v; }} }},
}};
global.document = {{ body: {{ appendChild: () => {{}} }} }};
global.window = {{ innerWidth: 1600*ZOOM, innerHeight: 1000*ZOOM }};
{re.sub(r"^  ", "", src, flags=re.M)}
placeReactionPicker(anchor, picker);
console.log(JSON.stringify({{left: parseFloat(style.left), top: parseFloat(style.top)}}));
""")
        out = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class TestThePickerLandsWhereItWasAimed(unittest.TestCase):
    def test_at_zoom_one_nothing_changes(self):
        """Every un-zoomed page: the two spaces are identical and the answer must not move."""
        at = place(1.0)
        self.assertAlmostEqual(at["left"], 1024 - 240, delta=1)

    def test_a_display_scale_does_not_push_it_off_screen(self):
        """The reported case. The style value must be the CSS position, not the scaled one."""
        # Within a couple of pixels: the clamp's own margins are computed in the scaled space, so
        # the two answers differ by a rounding residue and not by the zoom. Off-by-the-zoom is
        # ~250px here, which is what this is actually distinguishing.
        one, scaled = place(1.0), place(1.25)
        self.assertAlmostEqual(one["left"], scaled["left"], delta=3,
                               msg=f"the picker moves with the zoom: {one} vs {scaled}")
        self.assertAlmostEqual(one["top"], scaled["top"], delta=3)

    def test_it_stays_inside_the_page_at_a_display_scale(self):
        at = place(1.25)
        self.assertLess(at["left"] + 240, 1600 + 1, f"the picker hangs off the right edge: {at}")
        self.assertGreaterEqual(at["left"], 0)

    def test_an_anchor_with_no_layout_width_is_not_a_division_by_zero(self):
        at = place(1.25, anchor_css_width=0)
        self.assertTrue(at["left"] == at["left"], "NaN reached style.left")


if __name__ == "__main__":
    unittest.main()
