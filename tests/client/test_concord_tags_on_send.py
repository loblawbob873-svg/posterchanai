"""Sending a message with @somebody in it really does tag them.

Asked directly: "Tagging users in concord does not look like it actually tags the user, tab
autocomplete fills it out, but is it actually working!"

It is — but nothing proved it. `test_concord_mentions.py` covers only the RECEIVING side, and it
hands `notifyMentions` events whose p-tags the test itself wrote. Nothing anywhere exercised the
composer, so "does sending produce a tag" had no answer in this repository, which is a fair reason
not to believe it.

Two halves, because the composer has two ways to mean a person:

  * `typedMentionRecipients` resolves @handles in the TEXT against the room's participants, so a
    name typed by hand tags too. Driven here against the shipped function.
  * the send handler pushes ['P',pk] and ['p',pk] for every resolved person — the uppercase for
    CORD's own addressing and the lowercase so ordinary Nostr clients see the mention. Asserted at
    the source, because the handler is an inline onclick with no seam to call.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONCORD = (ROOT / "static" / "js" / "client" / "concord.js").read_text(encoding="utf-8")
RUNTIME = Path(__file__).resolve().parent / "concord_tag_send_runtime.mjs"


class TypedMentionsResolveToPeople(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(RUNTIME)], capture_output=True, text=True, timeout=120)
        if out.returncode:
            raise AssertionError("the mention runtime failed: " + (out.stderr or "")[-2000:])
        cls.got = json.loads(out.stdout.strip().splitlines()[-1])

    def test_a_typed_handle_tags_that_person(self):
        self.assertEqual(["a" * 64], self.got["typed"],
                         "typing @alice did not resolve to alice, so a hand-typed mention reaches "
                         "nobody: %s" % self.got["typed"])

    def test_a_handle_that_matches_nobody_tags_nobody(self):
        self.assertEqual([], self.got["stranger"],
                         "an unknown @handle resolved to somebody, which tags the wrong person")

    def test_an_email_like_string_is_not_a_mention(self):
        """`@` inside a word is not an address — matching it would tag people out of URLs."""
        self.assertEqual([], self.got["inWord"],
                         "text containing an embedded @ produced a mention")

    def test_a_nip05_handle_is_deliberately_not_a_mention(self):
        """CHECKED, THEN CORRECTED — this test first asserted the opposite and was wrong.

        `mentionAliases` accepts display_name, name and the pubkey; it does not accept a NIP-05
        local part. That looked like a gap until the autocomplete was read: `drawMentions` offers
        `display_name || name || pubkey.slice(0,12)` and never a nip05, so nothing the UI suggests
        can fail to resolve when typed back. Asserting nip05 support would have been asserting a
        feature the app does not claim, and would have "failed" against correct code."""
        self.assertEqual([], self.got["byNip05"],
                         "a NIP-05 local part now resolves; if that is intended, the autocomplete "
                         "must offer it too or the two halves disagree")

    def test_someone_with_no_name_is_still_mentionable(self):
        """What autocomplete falls back to — a pubkey prefix — must resolve when typed back."""
        self.assertEqual(["c" * 64], self.got["byPubkey"],
                         "the fallback handle the picker inserts does not tag anybody")


class TheSendPathCarriesTheTags(unittest.TestCase):
    def _send(self):
        start = CONCORD.index("send.onclick=async()=>{")
        return CONCORD[start:CONCORD.index("input.onkeydown", start)]

    def test_both_tag_forms_are_pushed(self):
        send = self._send()
        self.assertIn("mentionTags.push(['P',pk],['p',pk])", send,
                      "the composer stopped tagging mentioned people")

    def test_autocompleted_and_typed_mentions_both_count(self):
        send = self._send()
        self.assertIn("mentionRecipients", send,
                      "a tab-completed mention is no longer collected")
        self.assertIn("typedMentionRecipients(text", send,
                      "a hand-typed @name is no longer collected — only autocomplete would tag")

    def test_the_tags_reach_the_published_event(self):
        send = self._send()
        self.assertRegex(send, r"extraTags=\[[^\]]*\.\.\.mentionTags",
                         "mention tags are built and then not included in the published event")
        self.assertIn("publishCordMessage(p,room,state.channel,text,extraTags", send,
                      "extraTags no longer reaches the publisher")


if __name__ == "__main__":
    unittest.main()
