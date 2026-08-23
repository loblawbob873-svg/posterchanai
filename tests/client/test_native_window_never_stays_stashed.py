"""A native window the compositor has parked is shown again, whatever we remember sending it.

    "POosterchanOS bug: firefox is now a black screen window"
    "have to move to make it draw firefox"

Measured on the live machine: sway had firefox in the scratchpad (`visible: false`) while the shell
was still drawing its frame at exactly the rectangle sway reported. A frame over a parked window is a
black hole — and "have to move to make it draw" is the tell, because dragging is one of the few
things that runs a sync.

Two defects, and either alone reproduces it.

1. `_natSent` was read as truth. It is a record of INTENT, kept so an unchanged rectangle is not
   re-sent sixty times a second. Once it disagrees with sway nothing resolves it: the window is not
   in the stash plan, so no hide is sent; the rectangle has not "changed", so no place is sent; and
   `show` is only reached when we remember hiding it. The window stays parked for ever. sway is the
   authority, and it already publishes the fact — a stashed window sits on the `__i3_scratch`
   workspace — so nothing extra is asked for.

2. Every call recorded what it was about to do and then did it inside a catch that swallows. A
   refused `show` left the shell believing the window was placed. That is a latch set before the
   attempt it describes, which this codebase has been bitten by before.
"""
import unittest
from pathlib import Path

from tests.client.test_native_window_follows_its_frame import body, strip_comments

ROOT = Path(__file__).resolve().parents[2]
OS_JS = ROOT / "static" / "js" / "client" / "os.js"
WM_JS = ROOT / "desktop" / "wm.js"


class TheCompositorIsTheAuthority(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.os = strip_comments(OS_JS.read_text())
        cls.sync = body(cls.os, "async function nsync")

    def test_the_window_list_reports_a_parked_window(self):
        # `stashed:` is structure, so it is checked in stripped source; the workspace NAME is a
        # string literal, and strip_comments blanks those — so that half reads the raw file.
        self.assertIn("stashed:", strip_comments(WM_JS.read_text()),
                      "wm.js does not say which windows are parked, so the shell has no way to "
                      "notice that what it believes is on screen is not")
        self.assertIn("__i3_scratch", WM_JS.read_text())

    def test_the_sync_reconciles_against_it(self):
        self.assertIn("x.stashed", self.sync,
                      "nsync trusts its own record of what it sent; nothing ever corrects it")

    def test_a_parked_window_is_recorded_as_hidden(self):
        """Recording it as hidden is precisely what makes the placement pass call show()."""
        raw = body(OS_JS.read_text(), "async function nsync")
        i = raw.index("x.stashed")
        self.assertIn("'hidden'", raw[i:i + 200])


class NothingIsRecordedBeforeItSucceeds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sync = body(strip_comments(OS_JS.read_text()), "async function nsync")

    def test_hide_is_recorded_after_the_call(self):
        i = self.sync.index("pcWM.hide(")
        seg = self.sync[max(0, i - 160):i]
        self.assertNotIn("_natSent.set(it.native, 'hidden')", seg,
                         "the hide is recorded before it is attempted, so a refusal is remembered "
                         "as a success")

    def test_place_is_recorded_after_the_call(self):
        i = self.sync.index("pcWM.place(")
        seg = self.sync[max(0, i - 200):i]
        self.assertNotIn("_natSent.set(it.native, rect)", seg,
                         "the placement is recorded before it is attempted")

    def test_a_failed_show_stays_hidden_so_it_retries(self):
        """Clearing the record here would be worse than leaving it: with no entry the show branch is
        never reached again and the window is parked for good."""
        i = self.sync.index("pcWM.show(")
        seg = self.sync[i:self.sync.index("_natSent.delete(it.native)", i)]
        self.assertIn("catch", seg)
        self.assertIn("continue", seg)
        self.assertIn("_natSent.set(it.native", seg)

    def test_a_stashed_surface_is_shown_before_sway_is_asked_to_place_it(self):
        """Sway rejects floating/resize commands against a hidden scratchpad container."""
        self.assertLess(self.sync.index("pcWM.show("), self.sync.index("pcWM.place("))

    def test_a_failed_place_forgets_so_it_retries(self):
        i = self.sync.index("pcWM.place(")
        self.assertIn("_natSent.delete(it.native)", self.sync[i:i + 260],
                      "a refused placement is remembered as done, and an unchanged rectangle is "
                      "never re-sent")


class AStashedSurfaceIsNotDrawnAsABlackWindow(unittest.TestCase):
    def test_the_empty_frame_is_hidden_and_restored_with_the_surface(self):
        raw = OS_JS.read_text()
        css = (ROOT / "static" / "css" / "client.css").read_text()
        self.assertIn("it.w.el.classList.add('native-stashed')", raw)
        self.assertIn("it.w.el.classList.remove('native-stashed')", raw)
        self.assertIn(".osw.native-stashed{visibility:hidden", css)

    def test_application_fullscreen_is_not_cancelled_by_frame_placement(self):
        raw = OS_JS.read_text()
        adopt = body(raw, "async function adoptAll")
        # Native apps are no longer placed from a browser-side frame, so their fullscreen state is
        # owned by Sway and there is nothing here that can accidentally cancel it.
        self.assertIn("nativeTasks = rows", adopt)
        self.assertNotIn("pcWM.place", adopt)
        self.assertNotIn("pcWM.fullscreen", adopt)


if __name__ == "__main__":
    unittest.main()
