"""A Nostr mention must reach the fediverse as a REAL mention, however it was written.

Run: venv-unified/bin/python -m unittest tests.test_fedi_writeback_mentions

A mention arrives here two ways. Picking from the client's autocomplete inserts `nostr:npub…`, which
_translate_mentions has always rewritten. But typing `@choom` and NOT picking is also a valid mention:
the client resolves it against cached profiles and emits a `p` tag, deliberately leaving the text
alone (static/js/client/app.js mentionTags). Those notes used to cross-post the literal string
"@choom", which resolves to nobody off-instance — Pleroma rendered plain text with `tag: []`, no link
and no notification (seen on a real cross-post to detroitriotcity.com).

So the assertions are about the p-tag path, and about everything it must NOT touch: an already
fully-qualified `@user@host`, an email address, an unrelated `@word`, and — the reason a bare name is
translated only on a UNIQUE hit — two p-tagged people answering to the same name.
"""
import asyncio
import unittest

from app.services import fedi_nostr_writeback_service as W

CHOOM = "10621af37669cc51c540fa0a247312cfbf844710b6cf8e5f0a2b38315b251fca"
BOB = "aa" * 32
NOFEDI = "bb" * 32

HANDLES = {CHOOM: "@choom@parcero.casa", BOB: "@bob@example.org"}
NAMES = {CHOOM: {"choom"}, BOB: {"choom", "bob"}, NOFEDI: {"carol"}}


def translate(content, pubkeys):
    """_translate_mentions with its two lookups stubbed: pubkey→fedi handle (DB + verify_credentials)
    and pubkey→kind-0 names (a relay query). Both are exercised for real in production; here we only
    care about the rewriting decision they feed."""
    ev = {"content": content, "tags": [["p", pk] for pk in pubkeys]}

    async def handle(db, pk):
        return HANDLES.get(pk)

    async def names(pk):
        return NAMES.get(pk, set())

    real_h, real_n = W._fedi_handle_for_pubkey, W._nostr_names_for
    W._fedi_handle_for_pubkey, W._nostr_names_for = handle, names
    try:
        return asyncio.run(W._translate_mentions(None, ev))
    finally:
        W._fedi_handle_for_pubkey, W._nostr_names_for = real_h, real_n


class TestBareMention(unittest.TestCase):
    def test_reported_note(self):
        """The note that surfaced this: bare @choom + a p-tag, cross-posted to Pleroma."""
        self.assertEqual(
            translate("@choom you may like this new one\nhttps://poster.place/x", [CHOOM]),
            "@choom@parcero.casa you may like this new one\nhttps://poster.place/x")

    def test_start_middle_and_punctuation(self):
        for text, want in [
            ("@choom", "@choom@parcero.casa"),
            ("hey @choom, look", "hey @choom@parcero.casa, look"),
            ("ask @choom.", "ask @choom@parcero.casa."),   # trailing period stays OUTSIDE the handle
            ("line one\n@choom hi", "line one\n@choom@parcero.casa hi"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(translate(text, [CHOOM]), want)

    def test_ambiguous_name_is_left_alone(self):
        """Two p-tagged people answer to "choom" — aiming the mention at either is worse than
        leaving plain text, so it stays untranslated (mirrors the client's 1-unique-hit rule)."""
        self.assertEqual(translate("@choom hi", [CHOOM, BOB]), "@choom hi")

    def test_no_ptag_and_no_fedi_identity(self):
        self.assertEqual(translate("@choom hi", []), "@choom hi")
        self.assertEqual(translate("@carol hi", [NOFEDI]), "@carol hi")

    def test_never_mangles_non_mentions(self):
        for text, tags in [
            ("@choom@parcero.casa hi", [CHOOM]),        # already fully qualified
            ("mail me at foo@choom.com ok", [CHOOM]),   # email address, not a mention
            ("@nobody hi", [CHOOM]),                    # @word matching no p-tagged profile
            ("", [CHOOM]),
        ]:
            with self.subTest(text=text):
                self.assertEqual(translate(text, tags), text)


if __name__ == "__main__":
    unittest.main()
