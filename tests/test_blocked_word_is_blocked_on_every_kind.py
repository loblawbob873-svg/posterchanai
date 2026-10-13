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


# ---- the retroactive purge, run against the store that actually serves (PosterChanDB) ----------------------
# These used to read the SOURCE of the Postgres store's purge ("blocked_word(" present, " LIKE " absent). That
# store is gone; the rule is now held by running the purge on a real PcdbRelayStore and looking at what is left.

def _purge(events, words, *, preserve=(), direct=()):
    """Store `events` (origin 'wot', or 'direct' for ids in `direct`), run delete_by_words, return the ids left."""
    import asyncio

    from tests.relay_backends import pcdb_factory

    async def go(loop, make):
        store = make(loop, retention_days=0)
        store.set_preserve_pubkeys(preserve)
        for ev in events:
            assert await store.add_event(ev, origin="direct" if ev["id"] in direct else "wot"), ev["kind"]
        await store.delete_by_words(sorted(words))
        return {ev["id"] for ev in events if await store.has_event(ev["id"])}

    loop = asyncio.new_event_loop()
    try:
        with pcdb_factory() as make:
            return loop.run_until_complete(go(loop, make))
    finally:
        loop.close()


def _ev(kind, content, *, pubkey="a" * 64, tags=None, created_at=None):
    import time

    from tests.test_posterchandb import ev_id
    ev = {"pubkey": pubkey, "created_at": created_at or int(time.time()) - 600, "kind": kind,
          "tags": tags or [], "content": content, "sig": "0" * 128}
    ev["id"] = ev_id(ev)
    return ev


def test_the_retroactive_purge_uses_the_same_predicate_and_the_same_kinds():
    """A relay that refuses a word at the door and declines to remove the same word already inside
    is a relay with two different answers to one question.

    The purge once built its own SQL LIKE with `_` escaped as a literal -- a faithful copy of
    `blocked_word` only while that stayed a plain substring test. Now it reads content and asks the
    predicate, exactly as the language purge does; this runs it and compares with the door.
    """
    caught = [_ev(k, PAYLOAD, created_at=None) for k in (1, 6, 16, 1111, 30023)]
    # Case-insensitive like the door, and `_` is a literal: a LIKE-shaped copy would match "zonexpresence".
    shouty = _ev(1, "ZONE_PRESENCE again")
    lookalike = _ev(1, "zonexpresence is not the blocked word")
    ordinary = _ev(1, "an ordinary post")
    own_data = [_ev(k, PAYLOAD, tags=[["d", "pcai:note:1"]] if k == 30078 else []) for k in (30078, 1059, 4)]
    left = _purge(caught + [shouty, lookalike, ordinary] + own_data, WORDS)
    for ev in caught + [shouty]:
        assert ingest._content_blocked(ev, None, WORDS), "the door refuses it"
        assert ev["id"] not in left, "...so the purge must remove it (kind %d)" % ev["kind"]
    for ev in (lookalike, ordinary):
        assert not ingest._content_blocked(ev, None, WORDS)
        assert ev["id"] in left, "the door admits it, the purge must keep it: %r" % ev["content"]
    for ev in own_data:
        assert ev["id"] in left, "kind %d is never word-filtered -- the purge must not delete it" % ev["kind"]


def test_the_purge_still_spares_what_it_always_spared():
    """Broadening a purge is the thing this relay has lost data to before: local users' own writes,
    preserved authors, and a note a SURVIVING event still points at (deleting it orphans the thread)."""
    local = "b" * 64
    mine = _ev(1, "zone_presence from a local user", pubkey=local)
    direct = _ev(1, "zone_presence published here directly", pubkey="c" * 64)
    anchor = _ev(1, "zone_presence at the root of a thread")
    reply = _ev(1, "a clean reply", pubkey="d" * 64, tags=[["e", anchor["id"]]])
    doomed_root = _ev(1, "zone_presence root")
    doomed_reply = _ev(1, "zone_presence reply", pubkey="d" * 64, tags=[["e", doomed_root["id"]]])
    left = _purge([mine, direct, anchor, reply, doomed_root, doomed_reply], WORDS,
                  preserve=[local], direct={direct["id"]})
    assert mine["id"] in left, "a preserved (local) author's notes are spared"
    assert direct["id"] in left, "a direct write is spared"
    assert anchor["id"] in left and reply["id"] in left, "a note a surviving reply e-tags is spared"
    assert doomed_root["id"] not in left and doomed_reply["id"] not in left, (
        "a thread condemned top to bottom still goes")
