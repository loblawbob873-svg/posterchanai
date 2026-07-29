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


class FakeQuery:
    def filter(self, *a, **k):
        return self

    def first(self):
        return None            # no puppet — the mentioned user is a native Nostr account


class FakeDb:
    """Enough session for _fedi_handle_for_pubkey: the puppet lookup misses, commits are no-ops."""
    def query(self, *a, **k):
        return FakeQuery()

    def commit(self):
        pass

    def rollback(self):
        pass


class FakeUser:
    def __init__(self, acct=None):
        self.pleroma_acct = acct
        self.pleroma_instance_url = "https://parcero.casa"
        self.pleroma_access_token = "tok"


class TestHandleResolution(unittest.TestCase):
    """A lookup that ERRORED must not be remembered as "this person has no fediverse account".

    An unresolved handle doesn't degrade the mention, it DELETES it (_strip_nostr_refs removes the
    `nostr:npub…` outright), so one failed request to someone else's instance used to vaporise every
    mention of that person for the full 300s negative TTL — silently, since the exception was
    swallowed without a log line. Seen in production against parcero.casa.
    """

    def setUp(self):
        W._handle_cache.clear()
        W._handle_neg.clear()
        self._real = W._any_user_for_pubkey
        self.addCleanup(lambda: setattr(W, "_any_user_for_pubkey", self._real))

    def _resolve(self, user, verify):
        W._any_user_for_pubkey = lambda db, pk: user
        real = W.pleroma_service.verify_credentials
        W.pleroma_service.verify_credentials = verify
        try:
            return asyncio.run(W._fedi_handle_for_pubkey(FakeDb(), CHOOM))
        finally:
            W.pleroma_service.verify_credentials = real

    async def _boom(self, *a, **k):
        raise RuntimeError("connection reset")

    async def _ok(self, *a, **k):
        return {"acct": "dj"}

    def test_stored_acct_needs_no_network(self):
        """Once pleroma_acct is on the row the handle resolves offline — so a flaky instance can no
        longer take mentions down at all."""
        self.assertEqual(self._resolve(FakeUser("dj@parcero.casa"), self._boom), "@dj@parcero.casa")

    def test_network_answer_is_backfilled(self):
        user = FakeUser(None)
        self.assertEqual(self._resolve(user, self._ok), "@dj@parcero.casa")
        self.assertEqual(user.pleroma_acct, "dj@parcero.casa")   # one call per user EVER, not per boot

    def test_failure_gets_the_short_ttl(self):
        self.assertIsNone(self._resolve(FakeUser(None), self._boom))
        self.assertEqual(W._handle_neg[CHOOM][1], W._HANDLE_FAIL_TTL)

    def test_genuine_absence_gets_the_long_ttl(self):
        """No linked fedi account at all is a real answer — worth remembering for the full TTL."""
        self.assertIsNone(self._resolve(None, self._boom))
        self.assertEqual(W._handle_neg[CHOOM][1], W._HANDLE_NEG_TTL)


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
