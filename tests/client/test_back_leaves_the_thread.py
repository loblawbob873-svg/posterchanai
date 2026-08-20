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
        self.assertIn("history.replaceState({}, '', target)", self.src)

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
        self.assertLess(blk.index("delete _tlScrollMemo[view]"), blk.index("requestAnimationFrame"),
                        "the memo is cleared after the frame, so a re-render in between restores twice")

    def test_the_restore_waits_for_layout(self):
        """scrollTop cannot exceed a height that has not been laid out yet — set synchronously it
        clamps to whatever fits and the restore silently lands near the top."""
        blk = self.src[self.src.index("const want = _tlScrollMemo[view];"):][:600]
        self.assertIn("requestAnimationFrame", blk)

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
        i = self.src.index("function renderTimeline(view, reset){")
        self.assertIn("_drawTimeline(false)", self.src[i:i + 400],
                      "entering a timeline no longer starts at the top")
