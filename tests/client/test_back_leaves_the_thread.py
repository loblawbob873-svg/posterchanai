"""Back should leave a post, not walk through posts — and land you where you were reading.

"Scrolling, click post make a comment, click the back button on my phone and it takes me back to
some other random post instead of my home feed. Ideally it would also keep me where in the timeline
I was."

TWO FAULTS, ONE FLOW.

`openThread` pushed a history entry every time, including when the reader was ALREADY in a thread.
A post opens its root, an ancestor, the reply just written — each one a push — so the back button
walked a chain of posts nobody asked to revisit before it ever reached the feed. Back should leave
the thread; the address bar must still name what is on screen, so the fix is `replaceState` for
steps taken WITHIN a thread, not "stop updating the URL".

And nothing remembered the reading position. `#feed` is one scroll container shared by every view,
so returning to the timeline re-rendered it from the top with no memory there had ever been an
offset.
"""
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js"


class BackLeavesTheThread(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text()

    def test_nav_can_replace_as_well_as_push(self):
        self.assertRegex(self.src, r"function _navUrl\(path, replace\)")
        self.assertIn("history.replaceState(_navState(VIEW), '', target)", self.src)

    def test_a_step_within_a_thread_replaces_rather_than_stacking(self):
        """The chain of back presses through posts is exactly this push."""
        m = re.search(r"const _rep = \(VIEW === 'thread'\);([\s\S]{0,400}?)\n", self.src)
        self.assertIsNotNone(m, "openThread no longer decides push-vs-replace by where it was called from")
        block = self.src[self.src.index("const _rep = (VIEW === 'thread');"):][:400]
        self.assertIn("_navUrl('/'+NT().nip19.neventEncode", block)
        self.assertIn("_rep", block)

    def test_the_url_still_names_what_is_on_screen(self):
        """Replacing, not skipping. A thread whose URL stopped updating would share and reload as
        the wrong post — a worse bug than the one being fixed."""
        block = self.src[self.src.index("const _rep = (VIEW === 'thread');"):][:400]
        self.assertNotIn("if(!_rep)", block,
                         "the URL is only updated when pushing — a share or reload now names the "
                         "post you arrived from, not the one you are reading")

    def test_the_timeline_offset_is_saved_on_the_way_out(self):
        """It has to be read while the old view's nodes are still on screen: after `VIEW = v` there
        is nothing left to measure."""
        i = self.src.index("_tlScrollMemo[VIEW] = f.scrollTop")
        j = self.src.index("VIEW = v;", i - 900)
        self.assertLess(i, j, "the offset is read after the view has already changed")

    def test_it_is_restored_once_and_then_forgotten(self):
        """The live re-renders that fire as relay events arrive come back through renderTimeline. An
        offset that survived would yank a reader back up the feed every time somebody posted."""
        blk = self.src[self.src.index("const want = _tlScrollMemo[view];"):][:600]
        self.assertIn("delete _tlScrollMemo[view]", blk)
        self.assertLess(blk.index("delete _tlScrollMemo[view]"), blk.index("_putScroll"),
                        "the memo is cleared after the restore starts, so a re-render in between "
                        "restores twice")

    def test_the_restore_retries_until_it_takes(self):
        """ONE FRAME IS NOT ENOUGH ON EVERY ENGINE. `scrollTop = want` against a feed that has not
        been laid out clamps to whatever fits — near the top, which is the bug. Chromium had usually
        laid out by the next animation frame; Firefox had not, and the reply repaint kept landing at
        the top after everything else was fixed."""
        blk = self.src[self.src.index("const want = _tlScrollMemo[view];"):][:400]
        self.assertIn("_putScroll", blk)
        put = self.src[self.src.index("function _putScroll(want, ok, budget){"):][:1400]
        self.assertIn("setTimeout(put", put, "it gives up after a single attempt")
        self.assertIn("scrollHeight > f.clientHeight", put,
                      "it writes the offset before the content is tall enough to hold it")

    def test_the_retry_stops_when_the_reader_takes_over(self):
        """A loop that keeps forcing a position would fight somebody scrolling. Their own scroll ends
        it, and so does leaving the view."""
        put = self.src[self.src.index("function _putScroll(want, ok, budget){"):][:1400]
        self.assertIn("ok && !ok()", put, "it restores into a view the reader has already left")
        self.assertIn("Math.abs(f.scrollTop - last) > 2", put,
                      "it fights the reader for the scrollbar")

    def test_each_timeline_tab_keeps_its_own_place(self):
        """Home, Nostrverse and Trending are three different reading positions; restoring one into
        another is worse than restoring nothing."""
        self.assertIn("_tlScrollMemo[view]", self.src)
        self.assertIn("_tlScrollMemo[VIEW]", self.src)


if __name__ == "__main__":
    unittest.main()


class TheFeedDoesNotJumpToTheTop(unittest.TestCase):
    """"I can scroll down, make a post, and then it resets me at the top, there are several other
    scenarios where this happens too, but it'd be hard to purposefully find them."

    They are hard to find on purpose because the trigger is not anything the person did: EOSE — a
    relay's end-of-stream — redrew the timeline with preserveScroll=false. That arrives after
    publishing (the composer re-subscribes), after a relay reconnects, and after returning to a
    backgrounded tab. Every one of those is a moment somebody is mid-feed.

    Only a fresh ENTRY to a timeline should start at the top, and only a deliberate change of what
    is shown (the media toggle).
    """

    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text()

    def test_an_eose_redraw_keeps_the_readers_place(self):
        m = re.search(r"const markEosed = \(\)=>\{[^}]*_drawTimeline\((true|false)\)", self.src)
        self.assertIsNotNone(m, "markEosed changed shape — re-read this test")
        self.assertEqual(m.group(1), "true",
                         "an end-of-stream from a relay yanks the reader back to the top of the feed")

    def test_entering_a_timeline_still_starts_at_the_top(self):
        """The fix must not become 'never reset', or opening a feed drops you wherever the last one
        happened to be — one scroll container is shared by every view."""
        # Brace-matched: a fixed window is a test that breaks the next time somebody adds a comment
        # to the function, which is exactly what happened to this one.
        i = self.src.index("function renderTimeline(view, reset){")
        j = self.src.index("{", i)
        depth, k = 0, j
        while k < len(self.src):
            if self.src[k] == "{": depth += 1
            elif self.src[k] == "}":
                depth -= 1
                if depth == 0: break
            k += 1
        self.assertIn("_drawTimeline(false)", self.src[i:k],
                      "entering a timeline no longer starts at the top")

    def test_resume_preserves_a_card_not_only_a_pixel(self):
        """New events above the reader change what a raw scrollTop means."""
        draw = self.src[self.src.index("function _drawTimeline(preserveScroll){"):][:5200]
        self.assertIn("_tlAnchor(feed)", draw)
        self.assertIn("_restoreTlAnchor(feed, place)", draw)

    def test_resume_keeps_the_loaded_scrollback_tail(self):
        """A redraw after paging must not reconcile 400 loaded cards down to the newest 200."""
        draw = self.src[self.src.index("function _drawTimeline(preserveScroll){"):][:1200]
        self.assertIn("_tl.pages===0 ? 200 : _FEED_MAX_CARDS", draw)


class EveryRouteOutOfTheFeedRemembers(unittest.TestCase):
    """"if I scroll, click a post, read through the comments, then click home, I go back to the top."

    The first version of this saved the offset in switchView only — and `renderThread` and
    `renderProfileView` set VIEW themselves without going through it, so the commonest journey of
    the lot (scroll, open a post, read replies, press Home) saved nothing at all. One helper, called
    by every route that leaves a timeline.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text()

    def test_opening_a_post_or_a_profile_remembers_the_feed(self):
        # Anchored on text unique to the route being asserted. `VIEW='thread'` alone also matches the
        # deep-link branch four thousand lines earlier — matching the FIRST occurrence of a common
        # string is how a guard ends up asserting something nobody meant.
        for where in ("VIEW='thread'; _hidePill(); _clearNav();",
                      "async function renderProfileView(pk){"):
            with self.subTest(where=where):
                i = self.src.index(where)
                near = self.src[max(0, i - 260):i + 260]
                self.assertIn("_rememberTlScroll()", near,
                              "this route leaves the timeline without remembering where the reader was")

    def test_the_helper_reads_before_the_view_changes(self):
        """After VIEW moves there is nothing left to measure."""
        i = self.src.index("if(VIEW !== v) _rememberTlScroll();")
        j = self.src.index("VIEW = v;", i)
        self.assertLess(i, j)

    def test_tapping_the_view_you_are_on_gives_up_the_saved_place(self):
        """The user's own spec for when it SHOULD reset. Dropping the scroll without dropping the
        memo would restore the position they just discarded, on the next return."""
        i = self.src.index("$$('.nav-item[data-view]').forEach(b=> b.onclick = ()=>{")
        blk = self.src[i:i + 700]
        self.assertIn("delete _tlScrollMemo[v]", blk)
        self.assertIn("f.scrollTop = 0", blk)


class CommentingDoesNotThrowYouToTheTop(unittest.TestCase):
    """"when I comment on a post it scrolls me back to the top."

    Posting a reply does not redraw the thread directly — the reply comes back through the relay and
    something upstream repaints — and every repaint began with `feed.innerHTML = spinner`, which is
    scrollTop 0. From the reader's side that is the app throwing them to the top of a conversation at
    the exact moment they take part in it.

    Same rule the timeline needed: only ARRIVING somewhere starts at the top.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text()

    def test_a_repaint_of_the_same_thread_keeps_the_offset(self):
        i = self.src.index("async function renderThread(id, hints){")
        blk = self.src[i:i + 2200]
        self.assertIn("const _same = (renderThread._tok === id && VIEW === 'thread');", blk,
                      "renderThread cannot tell a repaint from an arrival")
        self.assertIn("_keepTop", blk)

    def test_arriving_at_a_thread_still_starts_at_the_top(self):
        """The fix must not become 'never reset', or opening a post drops you at whatever offset the
        previous one happened to have — one scroll container is shared by every view."""
        i = self.src.index("async function renderThread(id, hints){")
        blk = self.src[i:i + 2200]
        self.assertIn("if(_keepTop > 0)", blk,
                      "the restore is unconditional, so a freshly opened post inherits an offset")

    def test_it_does_not_re_save_the_feed_position_on_a_repaint(self):
        """`_rememberTlScroll` reads #feed, and during a thread repaint #feed holds the THREAD. Left
        unguarded it would overwrite the timeline's remembered offset with a position in a post."""
        i = self.src.index("async function renderThread(id, hints){")
        blk = self.src[i:i + 2200]
        self.assertIn("if(!_same) _rememberTlScroll();", blk)

    def test_the_restore_retries_rather_than_writing_once(self):
        """Same lesson as the timeline's: one frame is enough in Chromium and is not in Firefox."""
        i = self.src.index("async function renderThread(id, hints){")
        blk = self.src[i:i + 2600]
        self.assertIn("_putScroll(_keepTop", blk)


class RepaintingTheCurrentTimelineKeepsThePlace(unittest.TestCase):
    """"on desktop replying to a post is still bringing me to top of timeline."

    On the windowed desktop, focusing a window calls `switchView` with the view that window is
    ALREADY showing. So the save in switchView is skipped — `VIEW !== v` is false — `renderView(true)`
    reaches renderTimeline with reset, and the draw puts the reader at the top with no remembered
    offset to return to. Anything else that repaints the current timeline does the same.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text()

    def test_a_repaint_of_the_current_timeline_captures_the_offset_itself(self):
        i = self.src.index("function renderTimeline(view, reset){")
        blk = self.src[i:i + 2200]
        self.assertIn("if(VIEW === view){", blk,
                      "renderTimeline cannot tell a repaint of the current feed from an arrival")
        self.assertIn("_tlScrollMemo[view] = at", blk)

    def test_it_does_not_capture_when_arriving_from_another_view(self):
        """#feed is shared: coming from elsewhere it still holds the OLD view's content, and its
        scrollTop says nothing about this timeline."""
        i = self.src.index("function renderTimeline(view, reset){")
        blk = self.src[i:i + 2200]
        j = blk.index("_tlScrollMemo[view] = at")
        self.assertIn("VIEW === view", blk[:j], "the capture is not gated on already being here")

    def test_an_offset_already_remembered_wins(self):
        """A save made on the way OUT is the reader's real position. A repaint that happens after the
        feed has been redrawn to the top would otherwise overwrite it with 0-ish noise."""
        i = self.src.index("function renderTimeline(view, reset){")
        blk = self.src[i:i + 2200]
        self.assertIn("!(_tlScrollMemo[view] > 0)", blk)


class TheOffsetIsTakenBeforeTheFeedIsBlanked(unittest.TestCase):
    """THE ONE THAT MADE FOUR PREVIOUS ATTEMPTS LOOK RIGHT AND CHANGE NOTHING.

    `renderView` replaces the feed with a spinner and THEN dispatches to the view:

        if (reset && VIEW!=='admin') feed.innerHTML = '<div class="spinner"></div>';
        if (VIEW==='home' || VIEW==='global') return renderTimeline(VIEW, reset);

    Replacing the content collapses scrollHeight, so scrollTop is 0 from that line onwards. Every
    capture that ran inside renderTimeline therefore read zero, saved nothing, and the restore
    faithfully put the reader back at 0 — the bug wearing the fix's clothes. Reported four times,
    latest as "firefox just brought me back to the top after commenting".
    """

    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text()

    def test_the_capture_happens_before_the_spinner(self):
        blank = self.src.index("""feed.innerHTML = '<div class="spinner"></div>'""")
        cap = self.src.rindex("_tlScrollMemo[VIEW] = at", 0, blank)
        self.assertLess(cap, blank,
                        "the offset is read after the feed has been blanked, so it reads 0")

    def test_it_only_captures_for_a_timeline(self):
        """#feed is shared. Saving a Notes or Mail offset under a timeline key would restore into
        the wrong view."""
        blank = self.src.index("""feed.innerHTML = '<div class="spinner"></div>'""")
        blk = self.src[blank - 500:blank]
        self.assertIn("_TL_TABS.indexOf(VIEW) >= 0", blk)

    def test_it_only_captures_on_a_reset(self):
        """A non-reset render leaves the feed alone, so there is nothing to preserve against and the
        live offset is still correct."""
        blank = self.src.index("""feed.innerHTML = '<div class="spinner"></div>'""")
        blk = self.src[blank - 500:blank]
        self.assertIn("if(reset &&", blk)
