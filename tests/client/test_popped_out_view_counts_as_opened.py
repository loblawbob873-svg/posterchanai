"""A REAL COMPOSITOR WINDOW IS STILL A WINDOW, AND `null` CANNOT SAY SO.

On PosterChanOS a view can be popped out into its own compositor toplevel instead of an in-page
frame (`PCOSWin.open`). `openApp` then had no in-page window to hand back and returned `null` --
which every caller reads as "nothing was opened".

`app.js:openThread` is one of those callers, and its desktop branch is literally

    if(PCOS.openDoc('post:'+id, 'Post', 'i-note', ...)) return;

so a popped-out post fell straight through and rendered the thread into the BASE VIEW, replacing
the desktop behind it. Reported as "I clicked on a notification on my laptop just now, and all the
apps dissappear, then, the post window is unscrollable" -- both halves of that one sentence are
this: the desktop is gone because the page navigated, and the post is unscrollable because it was
painted into a page the desktop was still styling rather than into the window that did open.

INVISIBLE ON THE WEB. `PCOSWin` is never enabled in a browser, so that branch cannot run and every
browser-driven check passes -- scripts/check_notification_opens_a_post.py included, which is why it
is a companion to this file and not a replacement for it.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _fn(src, decl, stop):
    body = src[src.index(decl):]
    return body[: body.index(stop)]


class TestOpenDocReportsAPoppedOutWindow(unittest.TestCase):
    def test_it_does_not_answer_null_after_a_real_window_opened(self):
        body = _fn(OS_JS, "  function openDoc(", "  function openSystemSettings(")
        self.assertIn("poppedOut", body,
                      "openDoc still answers null when a real compositor window was opened")

    def test_the_marker_is_truthy_for_the_caller_that_matters(self):
        """`if(PCOS.openDoc(...)) return;` is the whole contract."""
        body = _fn(OS_JS, "  function openDoc(", "  function openSystemSettings(")
        m = re.search(r"return \{ poppedOut: true[^}]*\}", body)
        self.assertIsNotNone(m, body)

    def test_openapp_records_that_it_opened_a_real_window(self):
        body = _fn(OS_JS, "  function openApp(", "\n    const existing = wins.find(")
        self.assertIn("_openedReal = true", body,
                      "openApp cannot distinguish a successful pop-out from a failure")

    def test_the_flag_is_cleared_so_a_later_failure_cannot_inherit_it(self):
        """A latch set and never cleared is the recurring shape in this codebase; a stale `true`
        would make the NEXT openDoc claim it opened a window it did not."""
        body = _fn(OS_JS, "  function openDoc(", "  function openSystemSettings(")
        self.assertIn("_openedReal = false;", body)
        self.assertGreaterEqual(body.count("_openedReal = false"), 2,
                                "the flag is not cleared on both the way in and the way out")

    def test_the_marker_is_not_a_fake_window(self):
        """A caller that used it as a frame would be reaching into another renderer. It should fail
        visibly rather than paint into nothing."""
        body = _fn(OS_JS, "  function openDoc(", "  function openSystemSettings(")
        m = re.search(r"return \{ poppedOut: true,([^}]*)\}", body)
        self.assertIsNotNone(m)
        for forbidden in ("el:", "body:", "slot:", "render:"):
            self.assertNotIn(forbidden, m.group(1),
                             f"the marker pretends to be a window ({forbidden})")


class TestTheCallerThisProtects(unittest.TestCase):
    def test_open_thread_still_gates_its_desktop_branch_on_the_return(self):
        """If this ever stops being a truthiness test, the fix above stops mattering and this file
        is the only thing that would notice."""
        body = _fn(APP_JS, "  function openThread(", "\n  function ")
        self.assertRegex(body, r"if\(\s*PCOS\.openDoc\(", body[:400])
        self.assertIn("return;", body.split("PCOS.openDoc(", 1)[1][:200])


if __name__ == "__main__":
    unittest.main()
