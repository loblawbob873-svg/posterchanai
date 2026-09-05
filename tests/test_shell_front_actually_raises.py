"""ASKING TO BE IN FRONT MUST PUT THE SURFACE IN FRONT, NOT MERELY EXEMPT IT FROM THE NEXT SINK.

The desktop shell is an opaque full-output surface, so it is kept BELOW applications -- that is what
stopped it covering OBS and every popped-out window. The exception is a PosterChan window drawn
INSIDE that surface: while one is focused the desktop genuinely has to be in front, and the renderer
says so through `pc:wm:shell-front`.

That handler added the renderer to `_shellWantsFront` and did nothing else, which exempts it from
the NEXT sink and does nothing about the one that already happened. And one always has: the only way
to click a window drawn inside the surface is to click the SURFACE, that click is a focus, and
`sinkShellOnFocus` sinks on every focus. So the order was -- click, sunk, and only then the renderer
asks to be forward, to a process that records the wish and leaves it at the back.

Reported as "social is stuck behind terminal and can't move", and the same shape as the earlier "a
bunch of apps are stuck and won't close": the window is alive, focused, receiving the clicks, and
underneath an application.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
WM_WAYFIRE = (ROOT / "desktop/wm-wayfire.js").read_text(encoding="utf-8")
WM_SWAY = (ROOT / "desktop/wm.js").read_text(encoding="utf-8")


def handler():
    body = MAIN[MAIN.index("ipcMain.handle('pc:wm:shell-front'"):]
    return body[: body.index("ipcMain.handle('pc:wm:hide'")]


class TestTheHandlerRaises(unittest.TestCase):
    def test_wanting_the_front_does_something_to_the_compositor(self):
        body = handler()
        want = body[body.index("if(want)"): body.index("else")]
        self.assertIn("raiseShellSurfaces", want,
                      "asking for the front only records a wish; the surface stays where it was")

    def test_releasing_the_front_still_sinks(self):
        body = handler()
        rest = body[body.index("else"):]
        self.assertIn("sinkShellSurfaces", rest,
                      "the desktop would stay above applications after its window closed")

    def test_the_raise_is_the_inverse_of_the_sink(self):
        fn = MAIN[MAIN.index("function raiseShellSurfaces()"):]
        fn = fn[: fn.index("ipcMain.handle('pc:wm:shell-front'")]
        self.assertIn("keepBelow(id, false)", fn)
        sink = MAIN[MAIN.index("function sinkShellSurfaces()"):]
        sink = sink[: sink.index("\n}")]
        self.assertIn("keepBelow(id, true)", sink,
                      "the two directions no longer use one lever, so they can disagree")

    def test_it_only_raises_surfaces_that_asked(self):
        """Raising every shell surface would put the OTHER monitor's desktop over its apps too."""
        fn = MAIN[MAIN.index("function raiseShellSurfaces()"):]
        fn = fn[: fn.index("ipcMain.handle('pc:wm:shell-front'")]
        self.assertIn("_shellWantsFront.has", fn)

    def test_it_does_not_touch_keyboard_focus(self):
        """The shell already has focus -- it was just clicked. Focusing again races the gesture."""
        fn = MAIN[MAIN.index("function raiseShellSurfaces()"):]
        fn = fn[: fn.index("ipcMain.handle('pc:wm:shell-front'")]
        for forbidden in ("focus(", "activate("):
            self.assertNotIn(forbidden, fn)


class TestTheLeverExistsOnBothBackends(unittest.TestCase):
    def test_wayfire_can_undo_a_send_to_back(self):
        """`state:false` is what makes the raise possible at all; a boolean-less call could only
        ever sink."""
        line = [l for l in WM_WAYFIRE.splitlines() if "keepBelow(id" in l][0]
        self.assertIn("state:on!==false", line.replace(" ", ""))

    def test_sway_answers_false_and_is_left_alone(self):
        """Sway paints floating over tiled unconditionally, so the shell is structurally below and
        there is nothing to raise. Answering false is what lets main call this unconditionally."""
        self.assertIn("keepBelow(){ return Promise.resolve(false); }", WM_SWAY)


if __name__ == "__main__":
    unittest.main()
