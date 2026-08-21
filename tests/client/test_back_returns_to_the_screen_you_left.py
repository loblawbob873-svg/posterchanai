"""Back returns to the screen you were on, at the place you were reading.

THREE ROUNDS OF "should be fixed now" DID NOT FIX IT, and this is why.

  "Still broken. If you click on a post from your home feed or something, it shouldn't take you to
   the last post you viewed, it should take you to the last place you were, where you were (in this
   case in the timeline at a specific place)."
  "pressing back on a git issue brings you to social"

TWO FAULTS, neither of which the earlier push-vs-replace fix could reach.

**THE ORDINARY TAP ON A POST PUSHED NOTHING AT ALL.** The feed's click delegate called
`renderThread` directly, and renderThread swaps the view without touching history — so the single
commonest navigation in the app left no entry. Back then popped whatever entry happened to be
underneath, which was the last post opened through a path that DID push (a notification row, a quote
card): "it takes me back to some other random post instead of my home feed", exactly. Every fix
aimed at openThread's push-vs-replace rule was correct and inert, because this tap never reached
that rule. Measured before the fix, against the running client: tapping a card left
`location.pathname` and `history.length` unchanged.

**AND A SCREEN THAT IS NOT AN ENTITY HAD NO ENTRY TO COME BACK TO.** Notifications, Git, Messages,
Files, a repo's issue list — every one of them lives at the root path, and routeFromPath answers the
root path with `_startTimeline()`. So Back out of anything opened from one of them landed on Social
no matter where the reader had been. The view rides in the history entry's STATE now, beside the
feed offset, and the pop handler prefers the path only when the path actually names something.

Each test here was run against the pre-fix file and fails there.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "static" / "js" / "client" / "app.js"
GIT = ROOT / "static" / "js" / "client" / "git.js"


class EveryTapOnAPostIsAHistoryEntry(unittest.TestCase):
    """The bug that survived three fixes: the tap that never reached the rule."""

    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text()

    def test_the_feed_card_tap_goes_through_openThread(self):
        """`renderThread` renders; `openThread` navigates. A click handler must call the one that
        also leaves a history entry, or Back has nothing of this screen to return through."""
        m = re.search(r"if\(!btn\)\{ if\(art && art\.dataset\.id[^\n]*?\}", self.src)
        self.assertIsNotNone(m, "the feed's card-tap branch changed shape — re-read this test")
        self.assertIn("openThread(art.dataset.id)", m.group(0))
        self.assertNotIn("renderThread(art.dataset.id)", m.group(0),
                         "tapping a post in the feed pushes no history entry, so Back pops whatever "
                         "was underneath — the 'some other random post' report")

    def test_no_click_handler_reaches_renderThread_directly(self):
        """Four of them did: the card tap, a nostr: event link in a body, the search box's
        note1/nevent1 branch, and Enter on the keyboard-selected card. They are the same navigation
        and must leave the same trace."""
        for frag in ("closest('.evlink')", "d.type==='note'?d.data:(d.data&&d.data.id)"):
            i = self.src.index(frag)
            line = self.src[self.src.rindex("\n", 0, i) + 1: self.src.index("\n", i)]
            self.assertNotIn("renderThread(", line, f"still renders without navigating: {line.strip()[:90]}")
        blk = self.src[self.src.index("const tid = el.dataset.tid ||"):][:200]
        self.assertIn("openThread(tid)", blk)

    def test_routing_still_renders_without_pushing(self):
        """The other half of the same rule. openNaddr is reached FROM a pop, so its non-addressable
        fallback must keep rendering directly — pushing there would re-stack the entry we just
        popped and Back would never leave."""
        blk = self.src[self.src.index("async function openNaddr("):][:1400]
        self.assertIn("else renderThread(ev.id);", blk)


class AViewIsAHistoryEntry(unittest.TestCase):
    """"pressing back on a git issue brings you to social" — because every screen that is not an
    entity shared one URL, and that URL means "the timeline"."""

    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text()

    def test_switchView_records_the_view_not_just_the_root_path(self):
        self.assertIn("_navView(v);", self.src)
        self.assertNotIn("_navUrl('/');   // top-level views", self.src,
                         "switchView still resets the address bar to a path that means 'timeline'")
        blk = self.src[self.src.index("function _navView(v){"):][:1200]
        self.assertIn("history.pushState({ pcv:v, top:0 }", blk)

    def test_the_entry_carries_where_you_were_reading(self):
        blk = self.src[self.src.index("function _navState(view){"):][:500]
        self.assertIn("f.scrollTop", blk)
        self.assertIn("pcv:", blk)

    def test_leaving_a_screen_stamps_it_before_the_push(self):
        """The offset has to be written onto the entry we are LEAVING, while that screen is still on
        screen and its scrollTop is still real — the same rule the timeline memo needed."""
        blk = self.src[self.src.index("function _navUrl(path, replace){"):][:4000]
        i, j = blk.index("_stampHere()"), blk.index("history.pushState({}, '', target)")
        self.assertLess(i, j, "the entry is pushed before the screen it replaces is recorded")

    def test_a_pop_prefers_the_state_when_the_path_names_nothing(self):
        blk = self.src[self.src.index("window.addEventListener('popstate'"):][:900]
        self.assertIn("!_entityFromPath()", blk,
                      "the pop cannot tell an addressable entry from a bare one")
        self.assertIn("switchView(st.pcv)", blk,
                      "a popped entry is still answered with the timeline, whatever screen it held")
        self.assertIn("_restoreNavScroll(st)", blk)

    def test_a_pop_still_lets_the_path_win_when_it_names_something(self):
        """A post, a profile and a repo are addressable. Answering those from the state instead
        would open the wrong thing on a reload or a shared link."""
        blk = self.src[self.src.index("window.addEventListener('popstate'"):][:900]
        self.assertIn("routeFromPath()", blk)
        self.assertIn("st.pcv!=='thread'", blk)

    def test_boot_does_not_stack_entries_nobody_navigated_to(self):
        """Boot can switch the view two or three times before anybody sees the screen — the landing
        view, applyInstanceGating, a late synced pref. Each of those as an entry is a back press
        that goes nowhere."""
        blk = self.src[self.src.index("function _navView(v){"):][:1200]
        self.assertIn("!_userActed", blk)
        self.assertIn("history.replaceState({ pcv:v, top:0 }", blk)
        self.assertRegex(self.src, r"\['pointerdown','keydown','touchstart'\]")

    def test_the_scroll_restore_is_given_time_for_a_screen_that_refetches(self):
        """A popped repo re-reads its README across the internet. The default budget is one second;
        it costs nothing to wait longer, because _putScroll stops the moment the reader scrolls."""
        self.assertIn("function _putScroll(want, ok, budget){", self.src)
        blk = self.src[self.src.index("function _restoreNavScroll(st){"):][:600]
        self.assertRegex(blk, r"_putScroll\(st\.top, [^,]+, \d+\)")


class TheHardwareBackButtonCannotStrandYou(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text()

    def test_it_walks_our_own_entries_first(self):
        blk = self.src[self.src.index("if(typeof VIEW!=='undefined' && VIEW){"):][:600]
        self.assertIn("_navPushed>0", blk,
                      "history.back() is pressed with nothing of ours behind it — a Back that does "
                      "nothing at all")
        self.assertIn("history.back()", blk)

    def test_the_floor_is_the_timeline_the_app_opens_on(self):
        """"opening a post and clicking back always brings you back to the Home tab instead of
        obeying Timeline the app opens on"."""
        blk = self.src[self.src.index("if(typeof VIEW!=='undefined' && VIEW){"):][:1200]
        self.assertIn("_startTimeline()", blk)
        self.assertNotIn("VIEW!=='home'", blk, "the floor is hardcoded Home again")

    def test_there_is_always_a_way_out(self):
        """The double-tap exit has to remain reachable, or Back can never close the app.

        The floor switch must not push an entry of its own. switchView pushes now, so a floor that
        pushed would be popped back off by the next Back, landing on the screen underneath, whose
        Back lands on the floor again — a loop with the exit permanently one press away."""
        blk = self.src[self.src.index("if(typeof VIEW!=='undefined' && VIEW){"):][:1200]
        self.assertIn("__pcBackArmed", blk)
        floor = blk[blk.index("if(VIEW!==_home)"):][:160]
        self.assertIn("_routing=true", floor,
                      "the floor switch creates a history entry, so Back loops and never exits")


class ARepoComesBackToTheTabYouLeft(unittest.TestCase):
    """An issue is opened from the Issues tab and nowhere else, so coming back to the README is
    coming back to a screen the reader never chose."""

    @classmethod
    def setUpClass(cls):
        cls.src = GIT.read_text()

    def test_the_tab_is_remembered_per_repo(self):
        self.assertIn("const _rvTab = Object.create(null);", self.src)
        self.assertIn("_rvTab[_naddr]=tb.dataset.tab", self.src)

    def test_it_is_restored_only_on_a_back_press(self):
        """Arriving at a repo fresh from the list starts at its front page; only a pop resumes."""
        self.assertIn("function openRepo(e, opts){", self.src)
        blk = self.src[self.src.index("if(opts && opts.restore"):][:320]
        self.assertIn("_rvTab[_naddr]", blk)
        self.assertIn(".rv-tab[data-tab=", blk)

    def test_the_pop_is_what_asks_for_it(self):
        app = APP.read_text()
        self.assertIn("openRepo(ev, { restore:_routing })", app,
                      "openNaddr opens a repo without saying whether this was a back press")


if __name__ == "__main__":
    unittest.main()
