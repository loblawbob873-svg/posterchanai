"""THE PROCESS THAT PLACES A FLYOUT IS THE ONE THAT KNOWS WHERE THE TASKBAR IS.

Every flyout is positioned by the renderer, from the renderer's own measurements. MEASURED on the
real desk, twice, after two separate fixes to that arithmetic:

    start menu asked for   y=1072, height=1150   -> bottom edge 2222
    taskbar top edge       2500  (the shell's own published work area: reserve 60 on a 2560px
                                  output; the edge also found at y=2500 by scanning a screenshot)

A 278px gap -- "why is the start menu and taskbar widgets not attached to the dam taskbar still".
That arithmetic only works if the renderer believes its viewport is 2290 CSS px tall while its
compositor surface is 2560 physical, and no amount of correcting a renderer's sums fixes a renderer
whose idea of its own height is wrong.

So the number is applied in main: it placed the window, it knows the compositor rectangle, and it
holds the work area the shell MEASURED and published -- `reserve` being the taskbar, measured by the
only half that can see it. These tests run the shipped function.
"""
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")


def run(script):
    body = MAIN[MAIN.index("const _BAR_FLYOUTS = new Set("):]
    body = body[: body.index("async function placePopupWindow")]
    tmp = Path(tempfile.mkdtemp())
    try:
        js = tmp / "t.js"
        js.write_text("const _workAreas = new Map();\n" + body + "\n" + script)
        out = subprocess.run(["node", str(js)], capture_output=True, text=True, timeout=60)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# The real numbers off the desk: two 3840x2560 outputs whose taskbars measured 60px.
#
# `row.rect` IS THE POPUP'S OWN RECTANGLE, because that is what `wm().windows()` returns for it --
# a 975x1150 menu at (10,1072), not the output. A first version of this fixture used an
# output-shaped rect, which happened to match the lookup the code was doing and so passed against a
# version that moved nothing on the real machine. The fixture has to be the shape the caller
# actually passes or the test agrees with the bug.
SETUP = """
_workAreas.set('0,0', {x:0, y:0, w:3840, h:2500, reserve:60});
_workAreas.set('3840,0', {x:3840, y:0, w:3840, h:2500, reserve:60});
const row = {rect:{x:10, y:1072, width:975, height:1150}};
const out = (o) => console.log(JSON.stringify(o));
"""


class TestAFlyoutSitsOnTheBar(unittest.TestCase):
    def _y(self, script):
        r = run(SETUP + script)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_the_start_menu_is_moved_onto_the_bar(self):
        """The exact rectangle measured on the desk."""
        got = self._y("out(snapPopupToWorkArea({x:10, y:1072, w:975, h:1150}, row, 'start'));")
        self.assertEqual(got["y"] + got["h"], 2492,
                         f"the menu still does not reach the taskbar: {got}")
        self.assertEqual(got["h"], 1150, "it was resized; it must only be moved")
        self.assertEqual(got["x"], 10, "the horizontal placement was never wrong")

    def test_every_taskbar_flyout_is_snapped(self):
        for kind in ("start", "noti", "net", "tray"):
            got = self._y(f"out(snapPopupToWorkArea({{x:0, y:8, w:400, h:900}}, row, '{kind}'));")
            self.assertEqual(got["y"] + got["h"], 2492, f"{kind} was left off the bar: {got}")

    def test_a_flyout_on_the_second_monitor_uses_that_monitor_s_bar(self):
        """Each output publishes its own area; picking the wrong one moves the menu to the other
        screen's bar."""
        got = self._y("out(snapPopupToWorkArea({x:3850, y:1072, w:975, h:1150}, "
                      "{rect:{x:3850, y:1072, width:975, height:1150}}, 'start'));")
        self.assertEqual(got["y"] + got["h"], 2492, got)
        self.assertEqual(got["x"], 3850, "the flyout was moved horizontally")

    def test_one_that_already_sits_correctly_is_unchanged(self):
        got = self._y("out(snapPopupToWorkArea({x:0, y:1342, w:975, h:1150}, row, 'start'));")
        self.assertEqual(got["y"], 1342)

    def test_it_never_pushes_a_flyout_off_the_top(self):
        got = self._y("out(snapPopupToWorkArea({x:0, y:0, w:400, h:2490}, row, 'start'));")
        self.assertGreaterEqual(got["y"], 0)


class TestItLeavesEverythingElseAlone(unittest.TestCase):
    def _r(self, script):
        r = run(SETUP + script)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_the_composer_is_not_a_taskbar_flyout(self):
        """It is centred on the output; moving it to the bar is a correction nobody asked for."""
        got = self._r("out(snapPopupToWorkArea({x:100, y:600, w:900, h:950}, row, 'compose'));")
        self.assertEqual(got["y"], 600)

    def test_an_unmeasured_output_changes_nothing(self):
        """No published area means the taskbar was never measured -- guessing is worse than the
        renderer's own answer."""
        r = run("""
const row = {rect:{x:9999, y:0, width:1280, height:800}};
console.log(JSON.stringify(snapPopupToWorkArea({x:0, y:5, w:300, h:400}, row, 'start')));
""")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["y"], 5)

    def test_a_reserve_of_zero_changes_nothing(self):
        """`reserve:0` means the shell measured no taskbar on that output."""
        r = run("""
const _a = {x:0, y:0, w:3840, h:2560, reserve:0};
_workAreas.set('0,0', _a);
const row = {rect:{x:0, y:0, width:3840, height:2560}};
console.log(JSON.stringify(snapPopupToWorkArea({x:0, y:7, w:300, h:400}, row, 'start')));
""")
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["y"], 7)

    def test_a_flyout_taller_than_the_area_is_left_alone(self):
        got = self._r("out(snapPopupToWorkArea({x:0, y:0, w:400, h:9000}, row, 'start'));")
        self.assertEqual(got["y"], 0)


class TestThePlacementCallUsesIt(unittest.TestCase):
    def test_place_popup_snaps_before_it_commits(self):
        body = MAIN[MAIN.index("async function placePopupWindow"):]
        body = body[: body.index("ipcMain.handle('pc:popup:close'")]
        self.assertIn("snapPopupToWorkArea(want, row, _popupKind)", body)
        self.assertIn("placeAndReveal(Number(row.id), Math.round(put.x)", body,
                      "the snapped rectangle is computed and then not used")


if __name__ == "__main__":
    unittest.main()
