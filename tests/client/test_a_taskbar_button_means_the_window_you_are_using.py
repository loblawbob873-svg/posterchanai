"""A DESKTOP WINDOW'S TASKBAR BUTTON MUST NOT LOOK ACTIVE WHILE AN APPLICATION HAS THE KEYBOARD.

System Settings, Task Manager, Virtual Machines, Remote Desktop and folders are drawn INSIDE the
desktop surface, and such a frame keeps its `.focused` class for as long as it is open -- correctly,
since it is still the frame the desktop would hand the keyboard to. But the taskbar read that class
as "this is the window you are using", so with a popped-out window focused the button stayed lit,
and the click handler's

    if(w.el.classList.contains('focused') && !w.min) minimise(w); else focusWin(w);

then read the next press as "you are already here" and MINIMISED the window instead of raising it.

Measured on the laptop, clicking taskbar buttons and asking Wayfire after each one:

    click Social           -> Social focused        (right)
    click System Settings  -> desktop focused       (right)
    click Social           -> Social focused        (right)
    click System Settings  -> Social STILL focused  (wrong -- the press was a minimise)

The native half of the same handler had already learned the general lesson -- "the event-fed row can
lag the click by one compositor frame, so ask at the decision point" -- but for an in-page frame the
class is not one frame stale, it is answering a different question. So both the paint and the
decision go through one predicate that also asks whether this surface holds the keyboard.

With no compositor (a browser tab, the Windows and macOS builds) there is nothing to ask and the DOM
is the only truth, so the flag starts true and nothing changes there.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")


def _decls(text):
    return re.sub(r"/\*.*?\*/", "", text, flags=re.S)


class TestOnePredicate(unittest.TestCase):
    def test_there_is_one(self):
        self.assertIn("const _webTaskActive =", OS_JS,
                      "the paint and the click must not decide this separately")

    def test_it_asks_whether_this_surface_holds_the_keyboard(self):
        at = OS_JS.index("const _webTaskActive =")
        body = _decls(OS_JS[at: OS_JS.index(";", OS_JS.index("_shellHasKeyboard", at))])
        self.assertIn("_shellHasKeyboard", body)
        self.assertIn("classList.contains('focused')", body)
        self.assertIn("!w.min", body)

    def test_with_no_compositor_the_dom_is_still_the_truth(self):
        m = re.search(r"let _shellHasKeyboard = (\w+);", OS_JS)
        self.assertIsNotNone(m, "the flag is not declared with a starting value")
        self.assertEqual(m.group(1), "true",
                         "starting false would make every taskbar button inert in a browser, "
                         "in the Windows build and in the macOS build")


class TestBothUsesGoThroughIt(unittest.TestCase):
    def test_the_button_is_painted_from_it(self):
        at = OS_JS.index('data-kind="web"')
        markup = _decls(OS_JS[OS_JS.rindex("<button", 0, at): at])
        self.assertIn("_webTaskActive(w)", markup,
                      "the web task button still lights from the DOM class alone")
        self.assertNotIn("classList.contains('focused')", markup)

    def test_the_click_decides_from_it(self):
        at = OS_JS.index("minimise(w); else focusWin(w);")
        line = _decls(OS_JS[at - 200: at + 40])
        self.assertIn("_webTaskActive(w)", line,
                      "the click still minimises on the DOM class alone, so a window that lost the "
                      "keyboard to an application can be raised once and never again")


class TestTheFlagIsMaintained(unittest.TestCase):
    def test_the_pass_that_already_asks_the_compositor_sets_it(self):
        """It must ride the existing snapshot -- an extra IPC call per repaint is a call per tick."""
        at = OS_JS.index("_shellHasKeyboard = holds")
        near = _decls(OS_JS[at - 1200: at + 200])
        self.assertIn("shellId", near)
        self.assertIn("x.focused", near)
        self.assertIn("drawBar()", near,
                      "the flag changes what the bar draws, so a change must repaint it")

    def test_it_is_not_confused_with_a_foreign_application(self):
        """`_foreignFocused` answers 'somebody else has it'; this answers 'we have it'.

        Our own popped-out windows are not foreign, so the two are genuinely different and a single
        flag cannot serve both -- which is how the taskbar came to believe an in-page window was
        active while a PosterChan toplevel held the keyboard.
        """
        at = OS_JS.index("_shellHasKeyboard = holds")
        near = _decls(OS_JS[at - 1400: at + 400])
        self.assertIn("_foreignFocused", near, "the two flags are computed in the same pass")
        self.assertRegex(near, r"const holds =[\s\S]{0,200}?Number\(x\.id\) === shellId",
                         "holding the keyboard must be decided by the shell's OWN id")


if __name__ == "__main__":
    unittest.main()
