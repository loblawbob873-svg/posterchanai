"""THE DESKTOP IS NEVER RAISED. IT IS ORDERED.

This file used to pin the opposite rule, and that rule was wrong. `pc:wm:shell-front` answered "a
window I DRAW is focused" by exempting the desktop surface from the next sink -- i.e. by leaving an
OPAQUE, FULL-OUTPUT window over every application on that monitor. Clicking System Settings, Task
Manager, a folder or a post opened in its own window therefore took Telegram, Firefox and every
popped-out PosterChan window off the screen. Nothing was minimised, nothing moved, nothing was
logged. Reported (again) as "i don't want any windows hiding because I clicked another window!".

Sinking it instead is the same bug with the sign flipped -- the frame goes behind every application
and cannot be read or typed into ("System settings never gets focus", "social is stuck behind
terminal and can't move"). Both halves were approximating one ordinary rule, A COVERED WINDOW IS
COVERED AND ONLY A COVERED ONE, and neither could express it, because the desktop is ONE surface and
the right answer differs per application.

Wayfire can express it. `wm-actions/send-to-back` moves a view to the BOTTOM of the stack, so
sinking the desktop FIRST and then each window its focused frame actually overlaps leaves the
desktop above exactly those and below everything else. The renderer measures the overlap -- it is
the only half that knows where its frames are -- and this process owns the order.

MEASURED on the laptop over raw Wayfire IPC before any of this was written: every mapped toplevel
(the shell, a popped-out PosterChan window and TelegramDesktop) reports `layer: workspace`, focus
raises an ordinary toplevel over another, and `wm-actions/send-to-back` on Telegram photographed it
underneath the window it had been covering. There is no tiled/floating split to work around here;
that was sway.
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


def sink():
    body = MAIN[MAIN.index("async function sinkShellSurfaces()"):]
    return body[: body.index("\n}")]


class TestNothingCanRaiseTheDesktop(unittest.TestCase):
    def test_there_is_no_raise_at_all(self):
        """The one lever that put an opaque full-output window over every application is gone."""
        self.assertNotIn("function raiseShellSurfaces", MAIN)
        self.assertNotIn("keepBelow(id, false)", MAIN)

    def test_every_shell_surface_is_sunk_on_every_publish(self):
        self.assertIn("sinkShellSurfaces()", handler())
        self.assertIn("keepBelow(id, true)", sink())

    def test_the_alt_tab_failsafe_is_still_the_one_exception(self):
        """Alt+Tab shows its chooser by going compositor-fullscreen, which outranks the stack."""
        self.assertIn("_shellFullscreenFailsafes.has(id)", sink())


class TestOnlyTheCoveredWindowsGoUnder(unittest.TestCase):
    def test_the_handler_takes_a_list_of_ids(self):
        body = handler()
        self.assertIn("covers", body)
        self.assertIn("_shellCovers", body)

    def test_the_ids_are_validated_and_bounded(self):
        """They arrive from a renderer and are handed straight to the compositor."""
        body = handler()
        self.assertIn("Number.isSafeInteger", body)
        self.assertIn("slice(0, 64)", body)

    def test_a_bare_boolean_publishes_no_cover_list(self):
        """An older renderer must fail towards "nothing is hidden", never towards a pinned window."""
        body = handler()
        self.assertIn("Array.isArray(want)", body)

    def test_the_desktop_goes_down_before_the_windows_it_covers(self):
        """send-to-back means "to the BOTTOM", so the LAST call wins the lowest place. Reversed,
        the covered windows land above the very frame they are meant to be behind."""
        body = sink()
        self.assertLess(body.index("keepBelow(id, true)"), body.index("coveredViewIds()"),
                        "the covered windows are sunk before the desktop, which inverts the stack")

    def test_the_order_is_awaited(self):
        """Separate writes on one socket do not complete in the order they were issued."""
        body = sink()
        self.assertIn("await wm().keepBelow", body)
        self.assertNotIn("Promise.resolve(wm().keepBelow", body)

    def test_a_dead_renderer_stops_holding_windows_down(self):
        """A cover list outliving its renderer pins real applications under the desktop for ever,
        with no frame left on screen to explain it."""
        closed = MAIN[MAIN.index("created.on('closed'"):][:600]
        self.assertIn("_shellCovers.delete(contentsId)", closed)


class TestTheLeverExistsOnBothBackends(unittest.TestCase):
    def test_wayfire_sends_a_view_to_the_back(self):
        line = [l for l in WM_WAYFIRE.splitlines() if "keepBelow(id" in l][0]
        self.assertIn("wm-actions/send-to-back", line)

    def test_sway_answers_false_and_is_left_alone(self):
        """Sway paints floating over tiled unconditionally, so the shell is structurally below and
        there is nothing to order. Answering false is what lets main call this unconditionally."""
        self.assertIn("keepBelow(){ return Promise.resolve(false); }", WM_SWAY)


if __name__ == "__main__":
    unittest.main()
