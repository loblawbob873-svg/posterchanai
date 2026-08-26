"""One anatomy per post, and a repost that says who and when.

"Posts on the timeline need uniformity: these three posts all display different ways."
"when a repost is displayed, only the time of the original post is shown, but that AND when it was
 reposted [should be]."

MEASURED, not guessed. `scripts/check_timeline_uniformity.py` audits a real feed in a real browser;
on one Nostrverse load at 390px it found **11 of 63 cards drawing a two-line header** (43px instead
of 19px) and **4 of 4 reposts saying "someone reposted"**. This file pins the two fixes as source
rules so they cannot be undone by an unrelated CSS tidy; the browser check is what proves they work.

THE HEADER WRAPPED. `.hd` was `flex-wrap:wrap`, so a long display name pushed the handle and the
timestamp onto a second row — the same anatomy in two shapes, decided by how long a stranger's name
happens to be. It does not wrap now. Which field gives way took three measured attempts, each rejected by a
planted worst case (see the check script): the overflow goes entirely to the NAME, the handle is
CAPPED rather than shrunk — a shrinkable handle becomes an unreadable stub — and the time never
moves at all, because the timestamps' shared right edge is what makes the column read as a column.

THE REPOST HEADER NAMED NOBODY. The reposter's name was baked in as escaped text with no
`data-prof`, so when their kind-0 finally arrived `decorateProfiles` had nothing to patch and the
card said "someone reposted" for the rest of the session. It also carried no time at all: a repost
is two events with two timestamps, and the one that decides where it sits in the feed is the
repost's own.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "static" / "js" / "client" / "app.js"
CSS = ROOT / "static" / "css" / "client.css"
AUDIT = (ROOT / "scripts" / "check_timeline_uniformity.py").read_text()


class EveryHeaderIsOneLine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.css = CSS.read_text()

    def _rule(self, sel):
        m = re.search(re.escape(sel) + r"\{([^}]*)\}", self.css)
        self.assertIsNotNone(m, f"{sel} is gone — re-read this test")
        return m.group(1)

    def test_the_header_row_never_wraps(self):
        self.assertIn("flex-wrap:nowrap", self._rule(".note .hd"),
                      "a long display name drops the handle and the time onto a second row, so the "
                      "card is a different height from its neighbours")

    def test_the_name_gives_way_first(self):
        r = self._rule(".note .name")
        self.assertIn("text-overflow:ellipsis", r)
        self.assertIn("min-width:0", r, "a flex item cannot shrink below its content without this")

    def test_the_handle_is_capped_rather_than_shrunk(self):
        """Two shrinking rules were measured against planted worst cases and both left a stub:
        shrink 2 gave `Jay Blue Ribbon, Spiritual …` beside `s…`, and shrink 1 crushed `@17mugz59`
        to 29px beside a 63-character npub, because "in proportion" is not a fair fight when one
        field is seven times the other. The overflow goes entirely to the name; the handle gets a
        ceiling instead, which cannot produce a stub and cannot overflow the row."""
        name, handle = self._rule(".note .name"), self._rule(".note .handle")
        self.assertIn("text-overflow:ellipsis", handle)
        self.assertIn("flex:0 1 auto", name, "the name must be the field that absorbs the overflow")
        self.assertIn("flex:0 0 auto", handle, "a shrinkable handle is a handle that becomes a stub")
        self.assertRegex(handle, r"max-width:\d+%",
                         "with no ceiling a long nip05 crowds the name out — `bitcoinl…` beside "
                         "`bitcoinlimit@verified-nost…`")

    def test_the_timestamp_never_shrinks(self):
        r = self._rule(".note .time")
        self.assertIn("flex:none", r)
        self.assertIn("white-space:nowrap", r, "'2d' wrapping to two lines is the tall card again")

    def test_browser_audit_distinguishes_a_wrap_from_taller_inline_emoji(self):
        self.assertIn("nb.bottom < tb.top - 3 || tb.bottom < nb.top - 3", AUDIT)
        self.assertNotIn("Math.abs(nm.getBoundingClientRect().y - tm.getBoundingClientRect().y) > 3", AUDIT)


class ARepostSaysWhoAndWhen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text()
        cls.css = CSS.read_text()

    def test_there_is_exactly_one_builder(self):
        """Three places draw this header — the card, the not-yet-loaded placeholder, and the patch
        that replaces the placeholder. Hand-copied literals is how two of them lost the fix."""
        self.assertIn("function _repostTag(pk, ts){", self.src)
        self.assertEqual(1, len(re.findall(r'class="repost-tag"', self.src)),
                         "the repost header is spelled out in more than one place again")

    def test_the_reposters_name_can_be_filled_in_later(self):
        """'someone reposted' was on every repost in a measured feed. decorateProfiles fills
        `.name[data-prof]`; escaped text is not patchable by anything."""
        blk = self.src[self.src.index("function _repostTag(pk, ts){"):][:700]
        self.assertIn('class="name" data-prof=', blk)
        self.assertIn("'someone'", blk, "the placeholder before the profile lands is gone")

    def test_it_carries_the_time_of_the_repost(self):
        blk = self.src[self.src.index("function _repostTag(pk, ts){"):][:700]
        self.assertIn("timeAgo(ts)", blk)
        self.assertIn("rt-when", blk)

    def test_the_original_keeps_its_own_time(self):
        """Both times, not one replacing the other — the card below still describes the original."""
        blk = self.src[self.src.index("if (ev.kind===6){"):][:1400]
        self.assertIn("noteCard(orig, _repostTag(ev.pubkey, ev.created_at))", blk)

    def test_the_placeholder_carries_what_the_patch_will_need(self):
        """patchLoaded rebuilds the header when the original arrives and has no other route back to
        the kind-6, so the reposter and the repost time ride on the placeholder element."""
        blk = self.src[self.src.index("if (ev.kind===6){"):][:1600]
        self.assertIn("data-rtpk=", blk)
        self.assertIn("data-rtts=", blk)
        patch = self.src[self.src.index("function patchLoaded(e){"):][:600]
        self.assertIn("_repostTag(el.dataset.rtpk", patch)

    def test_the_repost_header_does_not_reintroduce_a_second_shape(self):
        m = re.search(r"\.note \.repost-tag\{([^}]*)\}", self.css)
        self.assertIsNotNone(m)
        self.assertIn("display:flex", m.group(1))
        self.assertIn("text-overflow:ellipsis", re.search(r"\.note \.repost-tag \.name\{([^}]*)\}", self.css).group(1))


if __name__ == "__main__":
    unittest.main()
