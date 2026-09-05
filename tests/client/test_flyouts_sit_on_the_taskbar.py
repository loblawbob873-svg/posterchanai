"""EVERY FLYOUT HANGS OFF THE TASKBAR, SO ITS Y COMES FROM THE BAR.

Each of the start menu, the notification centre, the network panel and the tray flyout derived its
vertical position from its own anchor BUTTON's `getBoundingClientRect()`. MEASURED on the real desk
with a display scale in force:

    start menu asked for   y=1076, height=1150   -> bottom edge 2226
    taskbar top edge       2500  (confirmed twice: the shell's published work area said
                                  `reserve: 60` on a 2560px output, and the edge was found at
                                  y=2500 by scanning a screenshot)

A 274px gap -- "the start menu and widgets not attached to the taskbar". An earlier fix corrected
the flyouts' SIZE (975x1150 measured, i.e. 780x920 at scale 1.25, which is right) and left the
position wrong, because size and position were coming from two different coordinate spaces.

Rather than settle which space that rect was in, the code now asks a question with one right
answer: THE TASKBAR'S BOTTOM IS THE VIEWPORT'S BOTTOM. A rect that agrees is in the space the
geometry is expressed in and is trusted; a rect that does not is ignored in favour of the bar's own
height. Both routes give the same answer on the real machine, and the second cannot be wrong about
which space it is in because it never leaves this one.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
SHELL_JS = (ROOT / "static/js/client/osshell.js").read_text(encoding="utf-8")


def _fn(src, decl, stop):
    body = src[src.index(decl):]
    return body[: body.index(stop)]


class TestThereIsOneAnswerForTheBarsEdge(unittest.TestCase):
    def test_a_named_helper_exists(self):
        self.assertIn("function taskbarTopPx()", OS_JS,
                      "each flyout still works out the bar's edge for itself")

    def test_it_trusts_a_rect_only_when_it_is_in_the_right_space(self):
        body = _fn(OS_JS, "  function taskbarTopPx(){", "\n  }")
        self.assertIn("window.innerHeight - r.bottom", body.replace(" ", "").join([""]) or body,
                      "nothing checks the rect against the viewport bottom")
        self.assertIn("r.top", body)

    def test_it_falls_back_without_a_rect_at_all(self):
        """A rect in the wrong space must not be used, and 'no rect' must still place the menu."""
        body = _fn(OS_JS, "  function taskbarTopPx(){", "\n  }")
        self.assertIn("window.innerHeight - popupPx(TASKBAR)", body)


class TestEveryFlyoutUsesIt(unittest.TestCase):
    def test_the_start_menu(self):
        body = _fn(OS_JS, "  function _startPopup(){", "\n  function toggleStart")
        self.assertIn("taskbarTopPx()", body)
        self.assertNotRegex(body, r"rect\.top\s*\)?\s*-\s*hD",
                            "the start menu still positions itself from the Start button's rect")

    def test_the_notification_centre(self):
        body = _fn(OS_JS, "  function _notiPopup(){", "pcPopup.toggle('noti'")
        self.assertIn("taskbarTopPx()", body)
        self.assertNotIn("r.top - hD", body)

    def test_the_network_panel(self):
        body = _fn(OS_JS, "  function _netPopup(){", "const state = netState();")
        self.assertIn("taskbarTopPx()", body)
        self.assertNotIn("r.top - hD", body)

    def test_the_tray_flyout(self):
        body = _fn(SHELL_JS, "  function openTrayWindow(anchor){", "pcPopup.toggle('tray'")
        self.assertIn("os-bar", body, "the tray flyout does not consult the taskbar")
        self.assertNotIn("r.top - h - 8", body,
                         "the tray flyout still positions itself from the tray chip's rect")


class TestTheHorizontalAxisIsLeftAlone(unittest.TestCase):
    """It was never wrong: the menu lines up with Start, the panels with their own chips. Changing
    it would be a correction nobody asked for."""

    def test_the_start_menu_still_lines_up_with_start(self):
        body = _fn(OS_JS, "  function _startPopup(){", "\n  function toggleStart")
        self.assertIn("rect.left", body)

    def test_the_panels_still_line_up_with_their_chips(self):
        for decl, stop in ((" function _notiPopup(){", "pcPopup.toggle('noti'"),
                           (" function _netPopup(){", "const state = netState();")):
            body = _fn(OS_JS, decl, stop)
            self.assertIn("r.right - wD", body)


if __name__ == "__main__":
    unittest.main()
