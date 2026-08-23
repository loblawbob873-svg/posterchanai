"""Opening a native app must not blank the window that holds the shared feed.

    "wtf I opened up foot and the posterchanOS terminal goes black with spinning circle"
    "my foot terminal wente black until i moved it"

Two separate defects, both measured on the machine with the debug port open rather than reasoned
about.

ONE — the spinner. `focusWin` repaints a window by calling `PC().switchView(w.view)` when it has no
`render` of its own. A native window's view name is `native:<con_id>`, which is an id and not a view;
`renderView` blanks `#feed` to a spinner BEFORE discovering there is nothing to draw. `#feed` belongs
to whichever window last claimed it, so adopting foot wrote a spinner into the Terminal:

    after opening the terminal:   feedParent=osw-body  spinner=false  wins=[Terminal]
    after launching foot:         feedParent=osw-body  spinner=true   wins=[Terminal, foot[native]]

TWO — the black native window. `adoptNative` runs a sync the moment it builds the frame, before the
browser has laid it out, so the body measures 0x0, `mapRect` returns null and the app is never placed
— left wherever the compositor first put it with our frame drawn elsewhere. Skipping was treated as
final, so it stayed until something else happened to sync. Moving it is one of those things.
"""
import unittest
from pathlib import Path

from tests.client.test_native_window_follows_its_frame import body, strip_comments

ROOT = Path(__file__).resolve().parents[2]
OS_JS = ROOT / "static" / "js" / "client" / "os.js"


class ANativeWindowDoesNotSwitchTheClientView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = strip_comments(OS_JS.read_text())
        cls.focus = body(cls.src, "function focusWin")

    def test_the_fallback_is_gated_on_owning_the_feed(self):
        self.assertIn("else if(!w.noFeed)", self.focus,
                      "a native window still hands its con_id to switchView, which blanks the feed "
                      "of whichever window is holding it")

    def test_a_windows_own_render_is_still_called(self):
        """A folder is noFeed too and paints itself; only the shared-feed fallback is skipped."""
        self.assertIn("if(w.render)", self.focus)
        self.assertLess(self.focus.index("if(w.render)"), self.focus.index("else if(!w.noFeed)"))

    def test_native_windows_are_declared_nofeed(self):
        """The gate is worth nothing if adoptNative stops passing it."""
        self.assertIn("openApp(view, nw.title || nw.app || 'App', 'i-grid', null, true)",
                      OS_JS.read_text())


class APlacementThatCouldNotBeMeasuredIsRetried(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = strip_comments(OS_JS.read_text())

    def test_the_skip_schedules_another_look(self):
        sync = body(self.src, "async function nsync")
        i = sync.index("mapRect(")
        self.assertIn("_natMeasureAgain()", sync[i:i + 400],
                      "a window that had no area when adopted is skipped for good")

    def test_the_retry_waits_for_a_layout(self):
        """The missing layout is the one the browser has not performed yet — an instant re-run
        measures the same zero."""
        fn = body(self.src, "function _natMeasureAgain")
        self.assertIn("requestAnimationFrame", fn)
        self.assertIn("setTimeout", fn)

    def test_it_is_bounded(self):
        """"No area" is also what a genuinely hidden window reports; an unbounded retry would poll
        the compositor for the life of the page."""
        fn = body(self.src, "function _natMeasureAgain")
        self.assertIn("_natRetry >= ", fn)

    def test_the_budget_resets_when_something_lands(self):
        """So a busy desktop never exhausts a budget meant for one stuck measurement."""
        sync = body(self.src, "async function nsync")
        self.assertIn("_natRetry = 0", sync)

    def test_only_one_retry_is_in_flight(self):
        fn = body(self.src, "function _natMeasureAgain")
        self.assertIn("if(_natRetryT", fn)

    def test_html_overlap_never_stashes_a_live_native_window(self):
        """A PosterChan frame is the native surface's owner, not an occluder. Feeding HTML frame
        rectangles into stashPlan made Firefox disappear or turn black whenever focus changed."""
        sync = body(self.src, "async function nsync")
        self.assertIn("stashPlan(items, overlayRects())", sync)
        self.assertNotIn("wins.filter(w => w.native == null)", sync)
        self.assertEqual(sync.count("overlayRects()"), 1)
        overlays = body(self.src, "function overlayRects")
        self.assertIn("el !== desk && el !== bar", overlays)


class MovingBetweenOutputsDoesNotCloseTheApplication(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = OS_JS.read_text()
        cls.src = strip_comments(cls.raw)

    def test_the_bridge_returns_scoped_and_global_liveness_separately(self):
        preload = (ROOT / "desktop" / "preload.js").read_text()
        main = (ROOT / "desktop" / "main.js").read_text()
        self.assertIn("pc:wm:snapshot", preload)
        self.assertIn("pc:wm:snapshot", main)
        self.assertIn("allIds", main)
        self.assertIn("_shellScopes", main)
        self.assertIn("_nativeOwners", main)
        self.assertIn("scopedWindows(e, rows)", main)

    def test_a_minimised_window_keeps_its_last_display_owner(self):
        main = (ROOT / "desktop" / "main.js").read_text()
        i = main.index("function scopedWindows")
        scoped = main[i:main.index("let _shellRecoveryWired", i)]
        self.assertIn("!row.stashed", scoped)
        self.assertIn("_nativeOwners.set", scoped)
        self.assertIn("row && row.stashed ? _nativeOwners.get(id)", scoped)

    def test_a_window_alive_on_another_output_is_detached_not_killed(self):
        adopt = body(self.src, "async function adoptAll")
        self.assertIn("pcWM.snapshot", adopt)
        self.assertIn("allIds.has", adopt)
        self.assertIn("killNative:", adopt)

    def test_detaching_skips_the_compositor_close(self):
        close = body(self.src, "function closeWin")
        self.assertIn("opts.killNative !== false", close)
        self.assertIn("pcWM.close", close)


if __name__ == "__main__":
    unittest.main()
