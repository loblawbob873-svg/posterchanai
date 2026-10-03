"""Drafts of a Nostr-only account are either really kept, or it is said that they are not (backlog #71).

With no account row, /client/drafts answered {"ok": true} to a SAVE and stored nothing, so the client
believed its drafts were synced while they existed on one device only. A member (a NIP-05 name this
node granted) now gets the same server-held storage key an account has; anyone else is told plainly.
"""
import json
import unittest
from unittest import mock

from tests.test_doc_endpoints_refuse_an_unreachable_relay import _Base, _Db, _LiveRelay
from app.services import nip05_access, nostr_store


class _NoAccount(_Base):
    RELAY = _LiveRelay

    def setUp(self):
        super().setUp()
        self.db = _Db(None)                                   # no User row: a Nostr-only account
        self.keys = []
        p = mock.patch.object(nostr_store, "npub_storage_seckey", lambda npub: self.keys.append(npub) or b"\x02" * 32)
        p.start()
        self.patches.append(p)

    def member(self, yes):
        async def is_member(pk):
            return yes
        p = mock.patch.object(nip05_access, "is_member", is_member)
        p.start()
        self.patches.append(p)


class AMember(_NoAccount):
    def test_a_members_drafts_are_really_saved(self):
        self.member(True)
        code, j = self.drafts(drafts=[{"id": "d1", "ts": 10, "text": "hello"}])
        self.assertEqual((200, True), (code, j["ok"]))
        self.assertEqual(["npub1test"], self.keys, "not stored under the member's own storage key")
        self.assertEqual(1, len(self.relay.writes), "the drafts save wrote nothing")
        d, payload = self.relay.writes[0]
        self.assertEqual("pcai:drafts", d)
        self.assertEqual(["d1"], [x["id"] for x in payload["drafts"]])


class NotAMember(_NoAccount):
    def test_a_save_is_never_answered_ok_when_nothing_is_kept(self):
        self.member(False)
        code, j = self.drafts(drafts=[{"id": "d1", "ts": 10, "text": "hello"}])
        self.assertFalse(j["ok"], "answered ok to a save that stored nothing")
        self.assertTrue(j.get("local_only"))
        self.assertEqual([], self.relay.writes)
        self.assertEqual([], self.keys, "a storage key was made for a stranger")

    def test_loading_still_answers_an_honest_empty_list(self):
        self.member(False)
        code, j = self.drafts()
        self.assertEqual((200, True, []), (code, j["ok"], j["drafts"]))


if __name__ == "__main__":
    unittest.main()
