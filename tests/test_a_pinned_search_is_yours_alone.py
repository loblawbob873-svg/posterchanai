"""A pinned search belongs to one account, re-runs as what you typed, and pins once.

Run: venv-unified/bin/python -m pytest tests/test_a_pinned_search_is_yours_alone.py

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

Pins live in the `saved_searches` DocTable on this node's relay (#161, they left Postgres), so every rule is
driven against the shipped relay (tests/app_tables_harness.py), and "the relay could not be asked" is its own
answer: never an empty list, never a second pin.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.services import saved_search_service as saved
from app.services.relay_reader import Unavailable
from tests.app_tables_harness import tables, shared_relay, no_relay, unmigrated, fresh_process_view  # noqa: F401

ME, THEM = SimpleNamespace(id=1), SimpleNamespace(id=2)


def create(user, q):
    return asyncio.run(saved.acreate_saved_search(None, user, q))


def listing(user):
    return asyncio.run(saved.alist_saved_searches(None, user))


def delete(user, sid):
    return asyncio.run(saved.adelete_saved_search(None, user, sid))


# ------------------------------------------------------------------ what gets stored

def test_the_search_verb_is_stripped_so_a_pin_does_not_double_up():
    for typed in ("search xrp news", "SEARCH xrp news", "  web search  xrp news",
                  "Web Search xrp news"):
        assert saved.normalize_query(typed) == "xrp news", f"{typed!r} would re-run as 'search {typed}'"


def test_a_command_is_never_mistaken_for_the_verb():
    """`pin images cats` must re-run the IMAGES command. Stripping a real command word here
    would silently run a different tool than the one that was pinned."""
    for typed in ("images cats", "screenshot poster.place", "researchsearch quantum",
                  "searching for a flat"):
        assert saved.normalize_query(typed) == typed.strip(), f"{typed!r} lost its command word"


def test_nothing_is_pinned_from_nothing(tables):
    for typed in ("", "   ", None):
        assert create(ME, typed) is None, f"{typed!r} was pinned as an empty search"


def test_a_bare_verb_with_no_terms_is_kept_as_typed(tables):
    """Measured, and left alone deliberately. The strip is `verb + \\s+ + terms`, so "search"
    on its own does not match and is pinned as a search FOR the word "search". That is a
    strange thing to pin and a harmless one — and the alternative, stripping a bare verb to
    nothing, means `pin search` silently does nothing at all, which is worse. Written down here
    so the next reader knows it was looked at rather than missed."""
    assert saved.normalize_query("search") == "search"
    assert create(ME, "search") is not None


def test_a_query_is_bounded(tables):
    row = create(ME, "x" * 5000)
    assert len(row.query) == saved._MAX_QUERY, "a pasted document became a saved-search row"


# ------------------------------------------------------------------ pinning once

def test_pinning_the_same_thing_twice_returns_the_same_row(tables):
    first = create(ME, "xrp news")
    again = create(ME, "search xrp news")
    assert first.id == again.id, "the same search pinned twice made two rows nobody can tell apart"
    assert len(listing(ME)) == 1


def test_two_people_pinning_the_same_thing_get_their_own(tables):
    mine = create(ME, "xrp news")
    theirs = create(THEM, "xrp news")
    assert mine.id != theirs.id, "two accounts were given one shared pin"


# ------------------------------------------------------------------ whose pin it is

def test_you_only_see_your_own_pins(tables):
    mine = create(ME, "mine")
    create(THEM, "theirs")
    assert [r.id for r in listing(ME)] == [mine.id]


def test_you_cannot_delete_somebody_else_s_pin(tables):
    """The `user_id` check on delete. Without it, any account removes anybody's pins by
    guessing an id — and a pin that simply is not there next time reads as a bug in the app."""
    theirs = create(THEM, "theirs")
    assert not delete(ME, theirs.id), "one account deleted another account's saved search"
    assert len(listing(THEM)) == 1


def test_you_can_delete_your_own(tables):
    mine = create(ME, "mine")
    assert delete(ME, mine.id)
    assert listing(ME) == []


def test_deleting_something_that_is_not_there_is_false_not_an_error(tables):
    assert not delete(ME, 424242)


# ------------------------------------------------------------------ on the relay (#161)

def test_newest_first_and_a_fresh_process_sees_the_same_pins(tables):
    a = create(ME, "older")
    b = create(ME, "newer")
    assert [r.id for r in listing(ME)] == [b.id, a.id], "pins are listed newest first"
    view = fresh_process_view("saved_searches")
    assert {k: r["query"] for k, r in view.all().items()} == {str(a.id): "older", str(b.id): "newer"}
    assert delete(ME, a.id)
    assert [r.id for r in listing(ME)] == [b.id]
    assert str(a.id) not in fresh_process_view("saved_searches").all(), "a deleted pin came back on reload"


def test_ids_are_integers_the_old_buttons_can_carry(tables):
    """Telegram's `pin:del:<id>` / `pin:run:<id>` buttons and `pin delete <id>` parse a decimal integer."""
    s = create(ME, "xrp")
    assert isinstance(s.id, int) and str(s.id).isdigit() and s.id < 2 ** 53


def test_an_unreachable_relay_is_unavailable_not_no_pins(no_relay):
    for call in (lambda: listing(ME), lambda: create(ME, "x"), lambda: delete(ME, 1)):
        with pytest.raises(Unavailable):
            call()


def test_before_the_migration_has_a_verdict_nothing_is_answered(unmigrated):
    """A pin list read before the one-time move from Postgres would be missing every old pin, and a
    `pin` would duplicate one; both answer "unavailable" instead."""
    with pytest.raises(Unavailable):
        listing(ME)
    with pytest.raises(Unavailable):
        create(ME, "x")
