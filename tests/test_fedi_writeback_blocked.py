"""Regression test: blocking a profile on the relay must also take it off the write-back whitelist.

Run: venv-unified/bin/python -m unittest tests.test_fedi_writeback_blocked

Blocking a pubkey (Admin → Relay "Blocked accounts" / POST /client/block) is the operator's abuse
lever, and for ordinary posts it settles the matter at ingest: kinds 1/6/7/5 are signed by their
author, so the relay rejects and purges them and nothing reaches the write-back subscription at all.

A NIP-17 DM is the exception, and it is the one that matters here. The kind-1059 gift wrap is signed
by an EPHEMERAL key, so no pubkey denylist can match it; the relay accepts it, and _handle_dm_reply
unwraps it and gates the real sender on _bridge_allowed_pubkeys(). A blocked account could therefore
go on pushing direct messages onto the fediverse under its own linked handle, indefinitely.

Pinned here:
  - a bridge-enabled user who is NOT blocked is on the whitelist (so this can't pass by blocking
    everyone)
  - blocking that user removes them from the whitelist AND from the mention-translation map
  - the denylist is parsed the way the relay parses it: npub or hex, comma or newline separated
"""
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, User
from app.services import fedi_nostr_writeback_service as wb
from app.services.nostr import nostr_service

PK = "b" * 64
OTHER = "c" * 64
INST = "https://detroitriotcity.com"


class BlockedPubkeyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine, tables=[User.__table__])
        self.Session = sessionmaker(bind=self.engine)
        db = self.Session()
        for pk, name in ((PK, "abusive"), (OTHER, "innocent")):
            db.add(User(username=name, password_hash="x", nostr_npub=nostr_service.npub_of(pk),
                        pleroma_enabled=True, pleroma_instance_url=INST,
                        pleroma_access_token="tok", fedi_bridge_enabled=True))
        db.commit()
        db.close()

    def _refresh(self, blocklist: str):
        """Run the real _refresh_allowed against the in-memory DB with `blocklist` configured."""
        wb._allowed_cache["at"] = -3600.0        # defeat the 30s throttle
        settings = {"nostr_relay_blocked_pubkeys": blocklist}
        with mock.patch("app.database.SessionLocal", self.Session), \
             mock.patch.object(wb.settings_store, "get",
                               side_effect=lambda k, d=None: settings.get(k, d)):
            wb._refresh_allowed()
        return wb._allowed_cache["set"], wb._allowed_cache["all_uid"]

    def test_unblocked_user_is_whitelisted(self):
        allowed, all_uid = self._refresh("")
        self.assertIn(PK, allowed)
        self.assertIn(PK, all_uid)

    def test_blocked_by_npub_is_dropped(self):
        allowed, all_uid = self._refresh(nostr_service.npub_of(PK))
        self.assertNotIn(PK, allowed, "a relay-blocked account can still write back to the fediverse")
        self.assertNotIn(PK, all_uid, "a relay-blocked account still resolves for mention translation")
        self.assertIn(OTHER, allowed, "blocking one account must not disable the bridge for everyone")

    def test_blocked_by_hex_is_dropped(self):
        allowed, _ = self._refresh(PK)
        self.assertNotIn(PK, allowed)
        self.assertIn(OTHER, allowed)

    def test_denylist_parsing_matches_the_relay(self):
        """npub or hex, comma or newline separated — the same spellings the relay thread accepts."""
        both = f"{nostr_service.npub_of(PK)}, {OTHER}"
        allowed, _ = self._refresh(both)
        self.assertEqual(frozenset(), allowed)
        with mock.patch.object(wb.settings_store, "get",
                               side_effect=lambda k, d=None: f"{PK}\n{nostr_service.npub_of(OTHER)}"):
            self.assertEqual({PK, OTHER}, wb._blocked_pubkeys())

    def tearDown(self):
        wb._allowed_cache.update({"at": -3600.0, "set": frozenset(), "uid": {}, "all_uid": {}})


if __name__ == "__main__":
    unittest.main()
