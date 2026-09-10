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

THE RAISE IS GONE, AND WITH IT THE RACE THIS FILE WAS WRITTEN FOR. Raising an opaque full-output
surface put it over every application on the monitor -- "i don't want any windows hiding because I
clicked another window!" -- so the desktop is now sunk unconditionally and the windows its focused
frame OVERLAPS are sunk after it, leaving it above exactly those. Main skips nothing, so nothing can
win a race against it, and `_shellFrontWish(true)` survives only to tell the bottom guard to keep
its focus-a-sibling fallback away from a frame somebody is typing into.

What is still checked here is the renderer half: focusWin must state the front outright rather than
deriving it from `_foreignFocused` (which is cleared LATER, so clicking an in-page window while a
foreign app held focus published nothing at all), it must measure and publish the cover list, and it
must clear that list the moment an application takes focus -- a cover list that outlives its frame
pins real windows under the desktop with nothing on screen to explain it. The compositor half is
tests/test_a_covered_window_is_covered_and_only_a_covered_one.py.
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
        self.assertIn("_shellFrontWish(true)", self.body,
                      "focusWin never tells main the desktop has a window of its own on screen")
        self.assertIn("_stackDomAboveNative(", self.body,
                      "focusWin no longer raises the shell for an in-page window")

    def test_an_application_taking_focus_ends_the_cover_list(self):
        """`covers` describes ONE in-page frame's overlap. The moment the thing you clicked is a
        compositor window of its own, no frame of ours is on top -- and a list left behind holds
        real applications under the desktop with nothing on screen still claiming the space."""
        self.assertIn("_shellCoverWish([])", self.body,
                      "focusing an adopted application leaves the last frame's windows pinned")

    def test_it_is_stated_not_derived(self):
        """`_publishShellFront` computes the wish from `_foreignFocused`, which is cleared LATER --
        from the adopt pass, after a compositor event and a snapshot round trip. So with a foreign
        app focused, clicking an in-page window published nothing at all: `want` was still false and
        equal to what was last sent. Focusing a window the desktop DRAWS is the front being needed;
        that is the thing that just happened, not an inference from a flag."""
        self.assertIn("_shellFrontWish(true)", self.body)
        at = self.body.index("_shellFrontWish(true)")
        self.assertNotIn("_publishShellFront()", self.body[:at],
                         "the derived form must not run first and latch _shellFrontSent to false")

    def test_the_wish_is_not_only_sent_from_the_repaint(self):
        """drawBar's call is a repaint's -- it cannot be the one that beats the focus event."""
        draw = _decls(_fn(OS_JS, "  function drawBar(){"))
        self.assertIn("_publishShellFront()", draw)          # still there, still cheap
        self.assertIn("_shellFrontWish(true)", self.body,
                      "only drawBar publishes, so the wish is always late")

    def test_every_form_goes_through_one_sender(self):
        """Three places publish this, and two of them used to write the dedup latch themselves --
        which is how one can latch the other out of ever sending. They share `_sendShellFront`
        now, so the latch has one writer and the payload has one shape."""
        for header in ("  function _shellFrontWish(want){",
                       "  function _shellCoverWish(ids){"):
            body = _decls(_fn(OS_JS, header))
            self.assertIn("_sendShellFront(", body, header)
            self.assertNotIn("_shellFrontSent", body, header + " writes the latch behind the sender")
        sender = _decls(_fn(OS_JS, "  function _sendShellFront(next){"))
        self.assertIn("pcWM.shellFront(next)", sender)
        self.assertIn("_shellFrontSent = null", sender,
                      "a failed IPC must clear the latch or the wish is never retried")


class TestTheCoverListIsMeasured(unittest.TestCase):
    def test_the_stack_pass_publishes_what_the_frame_overlaps(self):
        body = _decls(_fn(OS_JS, "  async function _stackDomAboveNative(w, focusToken){"))
        self.assertIn("domStackPlan(others,rect)", body)
        self.assertIn("_shellCoverWish(plan.hide)", body,
                      "the overlap is computed and thrown away")

    def test_it_no_longer_takes_applications_off_the_screen(self):
        """Minimising somebody's browser to show a Settings window is precisely "opening a new
        window hides all the other windows". Only the no-compositor fallback may still do it."""
        body = _decls(_fn(OS_JS, "  async function _stackDomAboveNative(w, focusToken){"))
        hide = body.index("pcWM.hide(id)")
        guard = body.index("typeof pcWM.shellFront==='function'")
        self.assertLess(guard, hide,
                        "pcWM.hide runs on a compositor that could simply be asked to reorder")

    def test_a_popped_out_window_is_not_exempt(self):
        """It is an ordinary toplevel like Telegram, and a frame drawn over it must go in front of
        it by the same means. Exempting everything sharing our app-id is "social is stuck behind
        terminal" seen from the other end -- only the desktop's OWN surface may be skipped."""
        body = _decls(_fn(OS_JS, "  async function _stackDomAboveNative(w, focusToken){"))
        line = [l for l in body.splitlines() if "const others=rows.map(" in l][0]
        self.assertIn("Number(r.id)===shellId", line)
        self.assertNotIn("poster", line, "every window sharing our app-id is exempted again")


if __name__ == "__main__":
    unittest.main()
