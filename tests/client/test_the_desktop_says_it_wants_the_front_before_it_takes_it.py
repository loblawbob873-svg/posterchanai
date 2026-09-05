"""AN IN-PAGE WINDOW COMES FORWARD BECAUSE THE WISH WAS SENT BEFORE THE FOCUS.

Reported twice, as two bugs: "Running Global then clicking on System Settings causes the windows to
conflict, System settings never gets focus", and "social is stuck behind terminal and can't move".

System Settings, Task Manager, Virtual Machines, Remote Desktop and folders are drawn INSIDE the
desktop surface -- they are not toplevels -- so the only way to put one in front is to raise the
shell. `focusWin` does that through `_stackDomAboveNative`, which ends in `pcWM.focus(shellId)`.

Main sinks the shell on EVERY focus event (`sinkShellOnFocus` -> `sinkShellSurfaces`) and skips only
the surfaces in `_shellWantsFront`, which is filled by `pc:wm:shell-front`. The renderer published
that wish from `drawBar`, at the END of focusWin -- after the focus had already gone out. So:

    focus(shell)  ->  main sinks it  ->  shellFront(true) arrives  ->  keepBelow(id, false)

and `wm-actions/send-to-back` with `state:false` CLEARS the always-below flag; it raises nothing.
The desktop stayed at the back with the window drawn on it. Measured on the laptop: open Social,
click System Settings, and Wayfire still reports view 174 (Social) focused with the shell behind it
while the frame is present and carries `focused`.

Both halves are checked here, because either one alone reads as correct:
  * the renderer must publish the wish BEFORE it sends the focus, and
  * main must really skip a surface that has asked -- if it sank it anyway the order would not
    matter and this test would be measuring a comment.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
MAIN_JS = (ROOT / "desktop/main.js").read_text(encoding="utf-8")


def _fn(src, header):
    """The body of a function, to its closing brace at the same indent."""
    i = src.index(header)
    indent = " " * (len(src[:i].rsplit("\n", 1)[-1]))
    end = src.index("\n" + indent + "}", i)
    return src[i:end]


def _decls(text):
    """Comments quote the very calls this test orders, so they must not be matched."""
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


class TestFocusWinOrder(unittest.TestCase):
    def setUp(self):
        self.body = _decls(_fn(OS_JS, "  function focusWin(w, render){"))

    def test_it_both_publishes_the_wish_and_focuses_the_shell(self):
        self.assertIn("_publishShellFront()", self.body,
                      "focusWin never tells main the desktop has a window of its own on screen")
        self.assertIn("_stackDomAboveNative(", self.body,
                      "focusWin no longer raises the shell for an in-page window")

    def test_the_wish_is_published_first(self):
        wish = self.body.index("_publishShellFront()")
        focus = self.body.index("_stackDomAboveNative(")
        self.assertLess(wish, focus,
                        "the shell is focused before main knows it must not be sunk; the sink wins "
                        "and `send-to-back state:false` cannot undo it")

    def test_the_wish_is_not_only_sent_from_the_repaint(self):
        """drawBar's call is a repaint's -- it cannot be the one that beats the focus event."""
        draw = _decls(_fn(OS_JS, "  function drawBar(){"))
        self.assertIn("_publishShellFront()", draw)          # still there, still cheap
        self.assertIn("_publishShellFront()", self.body,
                      "only drawBar publishes, so the wish is always late")


class TestMainHonoursTheWish(unittest.TestCase):
    def test_a_surface_that_asked_is_not_sunk(self):
        body = _decls(_fn(MAIN_JS, "function sinkShellSurfaces(){"))
        self.assertRegex(body, r"_shellWantsFront\.has\(.*?\)\)\s*continue",
                         "sinkShellSurfaces does not skip a surface that asked to be in front, so "
                         "publishing the wish first would change nothing")

    def test_asking_clears_the_always_below_flag(self):
        body = _decls(_fn(MAIN_JS, "function raiseShellSurfaces(){"))
        self.assertIn("keepBelow(id, false)", body,
                      "asking to be in front no longer clears the always-below state")

    def test_the_wish_is_recorded_before_the_raise_is_attempted(self):
        handler = _decls(MAIN_JS[MAIN_JS.index("ipcMain.handle('pc:wm:shell-front'"):][:600])
        add = handler.index("_shellWantsFront.add")
        raise_at = handler.index("raiseShellSurfaces()")
        self.assertLess(add, raise_at,
                        "raiseShellSurfaces reads the set, so recording after it is a no-op pass")


if __name__ == "__main__":
    unittest.main()
