"""A pinned search belongs to one account, re-runs as what you typed, and pins once.

Run: venv-unified/bin/python -m unittest tests.test_a_pinned_search_is_yours_alone

`app/services/saved_search_service.py` was on a coverage audit's list of modules no test or check
mentioned anywhere. It is small, which is why it is worth pinning rather than skipping: every rule
in it is one line, and a one-line rule is exactly what a refactor deletes without noticing.

  yours alone    `delete_saved_search` filters on `user_id`. Without it any signed-in account
                 removes anybody's pins by guessing an id, and the owner is never told — the row is
                 simply gone next time they look.
  what you typed the verb is stripped so a pin of `search xrp news` re-runs as `xrp news` and not
                 `search search xrp news`. ONLY the literal search verb: `images cats` is a command
                 and must survive verbatim, or the pin runs the wrong tool.
  once           pinning the same thing twice returns the existing row rather than growing a list
                 of duplicates nobody can tell apart.
  bounded        a query is capped, so a pasted document cannot become a row.
"""
import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import SavedSearch, User
from app.services import saved_search_service as saved


class APinnedSearchIsYoursAlone(unittest.TestCase):

    def setUp(self):
        engine = create_engine("sqlite://")
        User.__table__.create(engine)
        SavedSearch.__table__.create(engine)
        self.db = sessionmaker(bind=engine)()
        self.me = User(username="me", password_hash="x", nostr_npub="a" * 64)
        self.them = User(username="them", password_hash="x", nostr_npub="b" * 64)
        self.db.add_all([self.me, self.them])
        self.db.commit()
        # The pins are mirrored to the user's own encrypted records; that transport is somebody
        # else's test. Stubbed so this file measures the rules and not the relay.
        from app.services import record_store
        self._real = (record_store.mirror_search_blocking, record_store.delete_search_blocking)
        record_store.mirror_search_blocking = lambda *a, **k: None
        record_store.delete_search_blocking = lambda *a, **k: None
        self.addCleanup(self._restore)

    def _restore(self):
        from app.services import record_store
        record_store.mirror_search_blocking, record_store.delete_search_blocking = self._real
        self.db.close()

    # ------------------------------------------------------------------ what gets stored

    def test_the_search_verb_is_stripped_so_a_pin_does_not_double_up(self):
        for typed in ("search xrp news", "SEARCH xrp news", "  web search  xrp news",
                      "Web Search xrp news"):
            self.assertEqual(saved.normalize_query(typed), "xrp news",
                             f"{typed!r} would re-run as 'search {typed}'")

    def test_a_command_is_never_mistaken_for_the_verb(self):
        """`pin images cats` must re-run the IMAGES command. Stripping a real command word here
        would silently run a different tool than the one that was pinned."""
        for typed in ("images cats", "screenshot poster.place", "researchsearch quantum",
                      "searching for a flat"):
            self.assertEqual(saved.normalize_query(typed), typed.strip(),
                             f"{typed!r} lost its command word")

    def test_nothing_is_pinned_from_nothing(self):
        for typed in ("", "   ", None):
            self.assertIsNone(saved.create_saved_search(self.db, self.me, typed),
                              f"{typed!r} was pinned as an empty search")

    def test_a_bare_verb_with_no_terms_is_kept_as_typed(self):
        """Measured, and left alone deliberately. The strip is `verb + \s+ + terms`, so "search"
        on its own does not match and is pinned as a search FOR the word "search". That is a
        strange thing to pin and a harmless one — and the alternative, stripping a bare verb to
        nothing, means `pin search` silently does nothing at all, which is worse. Written down here
        so the next reader knows it was looked at rather than missed."""
        self.assertEqual(saved.normalize_query("search"), "search")
        self.assertIsNotNone(saved.create_saved_search(self.db, self.me, "search"))

    def test_a_query_is_bounded(self):
        row = saved.create_saved_search(self.db, self.me, "x" * 5000)
        self.assertEqual(len(row.query), saved._MAX_QUERY,
                         "a pasted document became a saved-search row")

    # ------------------------------------------------------------------ pinning once

    def test_pinning_the_same_thing_twice_returns_the_same_row(self):
        first = saved.create_saved_search(self.db, self.me, "xrp news")
        again = saved.create_saved_search(self.db, self.me, "search xrp news")
        self.assertEqual(first.id, again.id,
                         "the same search pinned twice made two rows nobody can tell apart")
        self.assertEqual(len(saved.list_saved_searches(self.db, self.me)), 1)

    def test_two_people_pinning_the_same_thing_get_their_own(self):
        mine = saved.create_saved_search(self.db, self.me, "xrp news")
        theirs = saved.create_saved_search(self.db, self.them, "xrp news")
        self.assertNotEqual(mine.id, theirs.id, "two accounts were given one shared pin")

    # ------------------------------------------------------------------ whose pin it is

    def test_you_only_see_your_own_pins(self):
        mine = saved.create_saved_search(self.db, self.me, "mine")
        saved.create_saved_search(self.db, self.them, "theirs")
        self.assertEqual([r.id for r in saved.list_saved_searches(self.db, self.me)], [mine.id])

    def test_you_cannot_delete_somebody_else_s_pin(self):
        """The `user_id` filter on delete. Without it, any account removes anybody's pins by
        guessing an id — and a pin that simply is not there next time reads as a bug in the app."""
        theirs = saved.create_saved_search(self.db, self.them, "theirs")
        self.assertFalse(saved.delete_saved_search(self.db, self.me, theirs.id),
                         "one account deleted another account's saved search")
        self.assertEqual(len(saved.list_saved_searches(self.db, self.them)), 1)

    def test_you_can_delete_your_own(self):
        mine = saved.create_saved_search(self.db, self.me, "mine")
        self.assertTrue(saved.delete_saved_search(self.db, self.me, mine.id))
        self.assertEqual(saved.list_saved_searches(self.db, self.me), [])

    def test_deleting_something_that_is_not_there_is_false_not_an_error(self):
        self.assertFalse(saved.delete_saved_search(self.db, self.me, 424242))


if __name__ == "__main__":
    unittest.main()
