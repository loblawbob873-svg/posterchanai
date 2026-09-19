"""THE RELAY REJECTS JSON-BLOB TIMELINE SPAM WITHOUT BREAKING LEGITIMATE JSON EVENTS.

Spammers flood the timeline with kind-1 notes whose whole content is a raw JSON object/array. The
relay now drops those — but ONLY kind 1, so the many event kinds whose content is legitimately JSON
(profiles, contacts, app data, DVM, and reposts, whose content IS the reposted event) are untouched.

`is_json_content` is a pure function (tested directly). `_content_blocked` is the shared accept
predicate used by every ingest path; the direct-publish path in server.py applies the identical
`kind == 1 and block_json and is_json_content(...)` rule (asserted against the source).
"""
import re
import unittest
from pathlib import Path

from app.services.nostr_relay.langfilter import is_json_content
from app.services.nostr_relay.ingest import _content_blocked

ROOT = Path(__file__).resolve().parents[1]


def note(content, kind=1):
    return {"kind": kind, "content": content, "pubkey": "a" * 64, "id": "b" * 64}


class WhatCountsAsJson(unittest.TestCase):
    def test_a_whole_json_object_or_array_is_json(self):
        for c in ('{"msg":"buy now"}', '[1,2,3]', '{}', '[]',
                  '  {"x": true}\n', '\n\n[{"a":1},{"b":2}]  ',
                  '{"kind":1,"content":"nested spam","tags":[]}'):
            self.assertTrue(is_json_content(c), f"should be JSON: {c!r}")

    def test_human_prose_is_not_json(self):
        for c in ("gm nostr", "check out {this} idea", "an array [like this] in a sentence",
                  "prices: {5, 10}", "here is json: {\"a\":1} — thoughts?",
                  "", "   ", "{", "[", "}{", "not json at all"):
            self.assertFalse(is_json_content(c), f"should NOT be JSON: {c!r}")

    def test_a_bare_scalar_is_not_json_spam(self):
        # Valid JSON, but ordinary note text — only objects/arrays are the spam shape.
        for c in ("5", "42", "true", "false", "null", '"quoted"', "3.14"):
            self.assertFalse(is_json_content(c), f"a scalar is a normal note: {c!r}")

    def test_a_code_fence_of_json_is_not_json(self):
        # Sharing JSON in a markdown fence is a human note; it does not start with { or [.
        self.assertFalse(is_json_content("```json\n{\"a\":1}\n```"))

    def test_bad_input_never_throws(self):
        for c in (None, "{unterminated", "[1,2", "{'single':'quotes'}"):
            self.assertFalse(is_json_content(c))


class TheAcceptDecision(unittest.TestCase):
    def test_a_kind1_json_note_is_rejected(self):
        self.assertTrue(_content_blocked(note('{"spam":"flood"}'), None, None))
        self.assertTrue(_content_blocked(note('[{"x":1}]'), None, None))

    def test_a_kind1_human_note_is_accepted(self):
        self.assertFalse(_content_blocked(note("gm, good morning nostr"), None, None))
        self.assertFalse(_content_blocked(note("look at {this}!"), None, None))

    def test_non_timeline_kinds_with_json_are_never_touched(self):
        # These kinds are LEGITIMATELY JSON — the whole point of "without breaking anything".
        for kind in (0,        # profile metadata
                     3,        # contacts
                     6,        # repost — content is the reposted event's JSON
                     16,       # generic repost
                     30078,    # app-specific data (settings, notes, budget, desktop, …)
                     10002,    # relay list
                     5300):    # a DVM request
            self.assertFalse(_content_blocked(note('{"anything":"json"}', kind=kind), None, None),
                             f"kind {kind} is legitimately JSON and must not be blocked")

    def test_the_toggle_off_lets_json_through(self):
        self.assertFalse(_content_blocked(note('{"spam":1}'), None, None, block_json=False))

    def test_json_block_is_independent_of_the_word_and_lang_filters(self):
        # A JSON note with no blocked words/langs is still caught by the JSON rule alone.
        self.assertTrue(_content_blocked(note('{"a":1}'), set(), set()))
        # And a plain note with nothing configured passes everything.
        self.assertFalse(_content_blocked(note("hello"), set(), set()))


class BothAcceptPathsAgree(unittest.TestCase):
    """The direct-publish path (server.py) must apply the SAME kind-1 JSON rule as the shared
    _content_blocked used by the sync/backfill paths — or spam blocked on one path arrives on the
    other."""
    def test_server_direct_publish_applies_the_json_rule_to_kind_1_only(self):
        src = (ROOT / "app/services/nostr_relay/server.py").read_text(encoding="utf-8")
        self.assertRegex(
            src,
            r"kind == 1 and self\.cfg\.get\(\"block_json\", True\) and is_json_content\(content\)",
            "server.py direct-publish does not apply the kind-1 JSON filter")

    def test_the_cfg_builder_reads_the_toggle_defaulting_on(self):
        src = (ROOT / "app/services/nostr_relay/thread.py").read_text(encoding="utf-8")
        self.assertIn('"block_json": gb("nostr_relay_block_json_posts", True)', src)


if __name__ == "__main__":
    unittest.main()
