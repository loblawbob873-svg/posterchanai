"""A blocked word is blocked whatever kind carries it.

Reported: "i added zone_presence to relay block words and the posts are still comeing through" —
and it WAS in the list, spelled exactly right. Both filters were written `kind == 1`, so anything
that was not a plain note was never examined, and the payload this was aimed at
(`{"type":"zone_presence","zone":…}`) arrives inside another kind entirely. Measured on the live
relay: events carrying that literal string are stored under kind 1 AND kind 6.

An operator typing a word into a block list is not saying "block this in kind 1". They are saying
"I do not want this on my relay".
"""
import json

from app.services.nostr_relay import ingest
from app.services.nostr_relay.langfilter import _NEVER_WORD_FILTERED
from app.services.nostr_relay.langfilter import blocked_word

WORDS = {"zone_presence"}
PAYLOAD = json.dumps({"content": json.dumps({"type": "zone_presence", "zone": "x"})})


def test_the_payload_is_caught_whatever_kind_wraps_it():
    for kind in (1, 6, 16, 42, 1111, 30023):
        assert ingest._content_blocked({"kind": kind, "content": PAYLOAD}, None, WORDS), kind


def test_an_ordinary_note_is_untouched():
    assert not ingest._content_blocked({"kind": 1, "content": "an ordinary post"}, None, WORDS)


def test_the_apps_own_datastore_is_never_word_filtered():
    """78/30078 carry settings, notes, calendars, contacts and the files index. A blocked word that
    happened to appear in one would refuse a user's own data — and the retroactive purge would
    delete it, which is the replaceable-document wipe this codebase has already paid for."""
    for kind in (78, 30078):
        assert kind in _NEVER_WORD_FILTERED


def test_ciphertext_is_never_word_filtered():
    """A match in an encrypted payload is impossible by construction; a false one silently drops
    somebody's direct message."""
    for kind in (4, 13, 1059):
        assert kind in _NEVER_WORD_FILTERED


def test_git_metadata_and_auth_are_protocol_not_content():
    for kind in (22242, 10318, 30617, 30618):
        assert kind in _NEVER_WORD_FILTERED


def test_both_filters_share_one_definition():
    """The direct-publish path and the sync path had their own lists, which is precisely how a word
    can be blocked and still arrive."""
    import inspect
    from app.services.nostr_relay import server
    for mod in (ingest, server):
        src = inspect.getsource(mod)
        assert "_NEVER_WORD_FILTERED" in src, mod.__name__
        assert "kind == 1 and blocked_word" not in src, mod.__name__


def test_language_detection_stays_kind_one_only():
    """It GUESSES, and it may only guess about prose. A repost's content is JSON."""
    langs = {"ru"}
    assert not ingest._content_blocked({"kind": 6, "content": "привет " * 20}, langs, None)


def test_a_punctuation_term_can_never_match_everything():
    """A block list is operator free text and some entries ARE punctuation.

    Separator folding was tried here and took the relay down: `--------------` is a real entry on
    this node, it folded to the empty string, and `"" in content` is true for every event ever
    published — so ingest refused everything and the purge was entitled to delete anything not
    preserved or anchored. Reported within the hour as "i am not seeing new posts, just reposts".
    """
    words = {"--------------", "\u2704-", "zone_presence"}
    assert blocked_word("good morning everyone", words) is None
    assert blocked_word("nothing to see", words) is None
    # …while the terms themselves still match exactly what they say.
    assert blocked_word("a -------------- rule", words) == "--------------"
    assert blocked_word('{"type":"zone_presence"}', words) == "zone_presence"


def test_the_retroactive_purge_uses_the_same_predicate_and_the_same_kinds():
    """A relay that refuses a word at the door and declines to remove the same word already inside
    is a relay with two different answers to one question.

    The purge built its own SQL LIKE with `_` escaped as a literal — a faithful copy of
    `blocked_word` while that was a plain substring test, and no longer one the moment separators
    began folding. There is no SQL form of the real predicate, so it reads content and asks the
    predicate, exactly as the language purge already does.
    """
    import inspect
    from app.services.nostr_relay.store import RelayStore
    src = inspect.getsource(RelayStore._delete_by_words_sync)
    # The CODE, not the docstring explaining what it used to do — prose about a bug is not the
    # bug. (Five tests went red today for asserting a spelling; this one nearly made six.)
    parts = src.split(chr(34) * 3)
    code = parts[2] if len(parts) >= 3 else src
    assert "blocked_word(" in code, "the purge must ask the same predicate the door asks"
    assert " LIKE " not in code, "a second, SQL-shaped definition of the same rule: " + code
    assert "_NEVER_WORD_FILTERED" in src and "kind NOT IN" in src, (
        "it was kind=1 only, exactly like the two ingest filters were")


def test_the_purge_still_spares_what_it_always_spared():
    """Broadening a purge is the thing this relay has lost data to before."""
    import inspect
    from app.services.nostr_relay.store import RelayStore
    src = inspect.getsource(RelayStore._delete_by_words_sync)
    assert "_preserve_clause()" in src, "local users' own writes are spared"
    assert "anchored" in src and "tag='e'" in src, (
        "a note a SURVIVING event still points at must not be deleted — that orphans the thread")
