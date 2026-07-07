"""Regression tests for the fediverse WRITEBACK federation decision (nostr → fediverse).

Run: venv-unified/bin/python -m unittest tests.test_fedi_writeback_decision

The bug (2026-07-07): a linked user's Nostr REPLY to a NATIVE Nostr post got cross-posted to the
fediverse as a standalone post — leaking the Nostr-side conversation out of context. Root cause:
`_is_reply` only recognized e-tags MARKED 'reply'/'root', so a reply carrying a deprecated POSITIONAL
NIP-10 e-tag (`["e", <id>]`, no marker) looked like a top-level post and federated. Four of the
admin's replies leaked before it was caught.

These pin the invariants the bridge must never regress:
  - ANY reply (marked OR unmarked positional) → _is_reply True  → NOT cross-posted (unless its parent
    is a bridged note, which is handled elsewhere by threading under it).
  - A genuine QUOTE-POST (NIP-18 q-tag, or a 'mention'-marked e-tag) → _is_reply False → still
    cross-posts as a top-level fediverse note.
  - _reply_parent_id resolves the DIRECT reply target, never the thread root when a distinct reply
    target exists, and skips 'mention' (quote) e-tags.
The exact tag-sets that leaked are included verbatim as regression fixtures.
"""
import unittest

from app.services.fedi_nostr_writeback_service import (
    _is_reply, _reply_parent_id, _referenced_event_ids, _strip_nostr_refs)

# The four real events that leaked (verbatim tag shapes) — all unmarked positional replies.
LEAKED = [
    {"tags": [["e", "676fbba64378ed539a9bba3ac32d42345f7c1214f5a1beef62e744e81333b524"],
              ["p", "14b55cd017eb033127ab4d0c8a50cd3d80dbaf4085e2ef3f13da9b1bf44831e6"]]},
    {"tags": [["e", "b6b6a7178f17" + "0" * 52], ["p", "aa" * 32]]},
    {"tags": [["e", "c7c7da85b1ed" + "0" * 52], ["p", "bb" * 32]]},
    {"tags": [["e", "25a475bddbc7" + "0" * 52], ["p", "cc" * 32]]},
]


class TestIsReply(unittest.TestCase):
    def test_leaked_unmarked_positional_replies_are_replies(self):
        for ev in LEAKED:
            self.assertTrue(_is_reply(ev), f"unmarked positional reply must be caught: {ev['tags']}")

    def test_marked_reply(self):
        self.assertTrue(_is_reply({"tags": [["e", "x", "", "reply"], ["p", "y"]]}))

    def test_marked_root_only(self):
        self.assertTrue(_is_reply({"tags": [["e", "root", "", "root"]]}))

    def test_top_level_no_etag(self):
        self.assertFalse(_is_reply({"tags": [["t", "nostr"], ["p", "y"]]}))

    def test_quote_qtag_with_mention_etag(self):
        # NIP-18 quote: q-tag + 'mention'-marked e-tag → NOT a reply → still cross-posts.
        self.assertFalse(_is_reply({"tags": [["q", "x"], ["e", "x", "", "mention"]]}))

    def test_quote_qtag_with_unmarked_etag(self):
        # A quote-post whose embed is an UNMARKED e-tag must not be misread as a reply.
        self.assertFalse(_is_reply({"tags": [["q", "x"], ["e", "x"]]}))

    def test_mention_marked_etag_only(self):
        self.assertFalse(_is_reply({"tags": [["e", "x", "", "mention"]]}))

    def test_reply_that_also_quotes_is_still_a_reply(self):
        self.assertTrue(_is_reply({"tags": [["e", "p", "", "reply"], ["q", "z"]]}))

    def test_empty_and_malformed_tags(self):
        self.assertFalse(_is_reply({"tags": []}))
        self.assertFalse(_is_reply({}))
        self.assertFalse(_is_reply({"tags": [["e"], ["e", ""]]}))   # e-tag with no id → ignored


class TestReplyParentId(unittest.TestCase):
    def test_reply_marker_wins(self):
        ev = {"tags": [["e", "root", "", "root"], ["e", "parent", "", "reply"]]}
        self.assertEqual(_reply_parent_id(ev), "parent")

    def test_root_only_is_the_parent(self):
        self.assertEqual(_reply_parent_id({"tags": [["e", "root", "", "root"]]}), "root")

    def test_unmarked_positional_last_etag(self):
        ev = {"tags": [["e", "older"], ["e", "parent"], ["p", "x"]]}
        self.assertEqual(_reply_parent_id(ev), "parent")

    def test_mention_etag_is_not_a_parent(self):
        # a lone 'mention' (quote) e-tag has no reply parent
        self.assertIsNone(_reply_parent_id({"tags": [["e", "q", "", "mention"]]}))

    def test_no_etags(self):
        self.assertIsNone(_reply_parent_id({"tags": [["p", "x"]]}))

    def test_leaked_event_parent_resolves(self):
        self.assertEqual(
            _reply_parent_id(LEAKED[0]),
            "676fbba64378ed539a9bba3ac32d42345f7c1214f5a1beef62e744e81333b524")


class TestHelpers(unittest.TestCase):
    def test_referenced_event_ids_prefers_reply(self):
        ev = {"tags": [["e", "root"], ["e", "parent", "", "reply"]]}
        self.assertEqual(_referenced_event_ids(ev)[0], "parent")

    def test_strip_nostr_refs(self):
        self.assertEqual(_strip_nostr_refs("hi nostr:npub1abc there"), "hi  there")
        self.assertEqual(_strip_nostr_refs(""), "")


if __name__ == "__main__":
    unittest.main()
