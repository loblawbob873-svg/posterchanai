"""A NIP-05 identity THIS NODE granted is what entitles its holder to the node's features.

Run: venv-unified/bin/python -m pytest tests/test_nip05_grants_access.py -q

Four surfaces used four unrelated mechanisms — `can_ai`, `can_image`, `can_music` and the shared
`blossom_whitelist` setting — reconciled only by the 15-minute `relay_access_policy` batch. Measured
on this deployment: of the 78 accounts holding a granted `poster.place` name AND publishing it,
**15 were refused AI, image generation and music generation**, every one of them because they had no
`User` row for the batch to grant anything to. Blossom was the only one of the four they passed, and
only because the batch had written them into the whitelist.

So the rules pinned here, each verified to FAIL against the pre-fix code:

  1. A confirmed member passes all four gates with no `User` row, no capability column and no
     whitelist entry — the grant IS the entitlement.
  2. A profile claim is NOT a grant. Anyone can write `alice@poster.place` into their own kind-0;
     an account this node never granted a name to gets nothing, however its profile reads. Reading
     that backwards is a privilege escalation, so it is asserted rather than assumed.
  3. The entitlement is ADD-ONLY. When the registry cannot be read, the membership check 503s or the
     switch is off, everyone who passed before still passes and nobody new does — a gate that can
     deny on an unreadable setting locks every member out of their own instance.
  4. Nothing here WRITES the Blossom whitelist. That list is shared and hand-edited, and this
     project has already lost it once to a rewrite from a stale read; membership is resolved per
     request from the registry instead.
"""
import asyncio
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, User
from app.services import blossom_service, nip05_access
from app.services.command_service.core import CommandService
from app.services.nostr import nostr_service as ns

MEMBER = "%064x" % 0xABC1          # granted a name here, and publishes it
SILENT = "%064x" % 0xABC2          # granted a name here, has NOT published it
IMPOSTOR = "%064x" % 0xABC3        # never granted anything; profile CLAIMS a poster.place name

REGISTRY = f"member {ns.npub_of(MEMBER)}\nsilent {SILENT}\n"


def _run(coro):
    return asyncio.run(coro)


class Base_(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine, tables=[User.__table__])
        self.db = sessionmaker(bind=self.engine)()
        self.settings = {"nostr_relay_nip05_names": REGISTRY,
                         "nostr_relay_nip05_domain": "poster.place",
                         "blossom_whitelist": ""}
        # Every cache in the path is keyed on the raw setting value or a wall clock; a test that
        # inherits one from the previous test measures the cache, not the rule.
        nip05_access._granted_cache.update(raw=None, set=frozenset())
        nip05_access._members.clear()
        blossom_service._whitelist_cache.update(ts=0.0, val=None, set=frozenset())
        blossom_service._operator_cache.update(ts=0.0, set=frozenset())

        def get(key, default=None):
            v = self.settings.get(key)
            return default if v is None else v

        # settings_store is ONE module object shared by nip05_access and blossom_service, so this
        # patch covers the registry read and the whitelist read alike.
        self.p_get = mock.patch.object(nip05_access.settings_store, "get", get)
        self.p_get.start()
        self.addCleanup(self.p_get.stop)

        # Stand in for instance_membership: the node's own answer to "did they publish the exact
        # address we granted them?". MEMBER did, SILENT did not, IMPOSTOR is not in the registry at
        # all (so the real checker never even reaches a relay for them).
        async def status(pubkey, force=False):
            pk = (ns.to_pubkey_hex(pubkey) or "").lower()
            return {"pubkey": pk, "qualified": pk == MEMBER,
                    "address": "member@poster.place" if pk == MEMBER else "",
                    "profile_address": "alice@poster.place" if pk == IMPOSTOR else "",
                    "reason": "qualified" if pk == MEMBER else "profile_mismatch"}

        from app.services import instance_membership
        self.p_status = mock.patch.object(instance_membership, "status", status)
        self.p_status.start()
        self.addCleanup(self.p_status.stop)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _user(self, pk, **caps):
        # id 100+, deliberately: `_user_has_capability` exempts id == 1 (the founding account), so a
        # test whose first row lands on id 1 measures that exemption and not this rule.
        u = User(id=100 + self.db.query(User).count(),
                 username="u" + pk[:8], password_hash="unused", nostr_npub=ns.npub_of(pk),
                 is_admin=False, **({"can_ai": False, "can_image": False, "can_music": False,
                                     "can_blossom": False} | caps))
        self.db.add(u)
        self.db.commit()
        return u

    def _cmd(self, user):
        # The real method, without constructing CommandService's search/chat services.
        svc = CommandService.__new__(CommandService)
        svc.db, svc.user, svc.is_bot = self.db, user, False
        return svc


class TestTheGrantIsTheEntitlement(Base_):
    def test_blossom_accepts_a_member_with_no_row_and_no_whitelist_entry(self):
        self.assertFalse(blossom_service.is_pubkey_allowed(self.db, MEMBER),
                         "precondition: nothing but membership may be letting them in")
        self.assertTrue(_run(blossom_service.is_pubkey_allowed_async(self.db, MEMBER)))

    def test_ai_chat_accepts_a_member_without_can_ai(self):
        u = self._user(MEMBER)
        self.assertFalse(u.can_ai)
        self.assertTrue(_run(nip05_access.ai_allowed(u)))

    def test_get_ai_user_lets_a_member_through(self):
        """The shipped FastAPI dependency, not a re-implementation of its rule."""
        from app.auth import get_ai_user
        u = self._user(MEMBER)
        self.assertIs(_run(get_ai_user(u)), u)

    def test_image_and_music_accept_a_member_without_the_columns(self):
        svc = self._cmd(self._user(MEMBER))
        for attr in ("can_image", "can_music"):
            self.assertFalse(svc._user_has_capability(attr), attr)
            self.assertTrue(_run(svc._user_may(attr)), attr)

    def test_a_member_with_no_account_at_all_still_uploads(self):
        """The measured failure: 15 confirmed members had no `User` row, so there was nothing for
        an admin or the reconcile to tick."""
        self.assertEqual(self.db.query(User).count(), 0)
        self.assertTrue(_run(blossom_service.is_pubkey_allowed_async(self.db, MEMBER)))


class TestAProfileClaimIsNotAGrant(Base_):
    def test_an_unregistered_pubkey_gains_nothing_from_claiming_our_domain(self):
        self.assertFalse(nip05_access.is_granted(IMPOSTOR))
        self.assertFalse(_run(nip05_access.is_member(IMPOSTOR)))
        self.assertFalse(_run(blossom_service.is_pubkey_allowed_async(self.db, IMPOSTOR)))
        u = self._user(IMPOSTOR)
        self.assertFalse(_run(nip05_access.ai_allowed(u)))
        self.assertFalse(_run(self._cmd(u)._user_may("can_image")))

    def test_is_granted_reads_the_registry_and_never_a_profile(self):
        self.assertTrue(nip05_access.is_granted(MEMBER))
        self.assertTrue(nip05_access.is_granted(SILENT))
        self.assertEqual(nip05_access.granted_pubkeys(), frozenset({MEMBER, SILENT}))

    def test_a_granted_name_that_was_never_published_is_not_yet_a_member(self):
        """Same predicate as relay_access_policy, so the gate and the reconcile cannot disagree."""
        self.assertTrue(nip05_access.is_granted(SILENT))
        self.assertFalse(_run(nip05_access.is_member(SILENT)))
        self.assertFalse(_run(blossom_service.is_pubkey_allowed_async(self.db, SILENT)))


class TestItCanOnlyEverGrant(Base_):
    def test_the_switch_off_restores_the_per_account_gates_exactly(self):
        self.settings["nip05_grants_access"] = "false"
        u = self._user(MEMBER)
        self.assertFalse(_run(nip05_access.ai_allowed(u)))
        self.assertFalse(_run(self._cmd(u)._user_may("can_music")))
        self.assertFalse(_run(blossom_service.is_pubkey_allowed_async(self.db, MEMBER)))

    def test_a_blank_stored_value_reads_as_ON(self):
        """settings_store.get_bool maps "" to False; a blank row saved by the admin form would
        otherwise switch every member's access off node-wide with nothing to say so."""
        self.settings["nip05_grants_access"] = ""
        self.assertTrue(nip05_access.enabled())
        self.assertTrue(_run(blossom_service.is_pubkey_allowed_async(self.db, MEMBER)))

    def test_an_unreadable_registry_denies_nobody(self):
        self.settings["nostr_relay_nip05_names"] = ""
        nip05_access._granted_cache.update(raw=None, set=frozenset())
        self.settings["blossom_whitelist"] = ns.npub_of(SILENT)
        self.assertFalse(_run(blossom_service.is_pubkey_allowed_async(self.db, MEMBER)))
        self.assertTrue(_run(blossom_service.is_pubkey_allowed_async(self.db, SILENT)),
                        "a whitelisted pubkey must not lose access because the registry is unreadable")
        self.assertTrue(_run(nip05_access.ai_allowed(self._user(IMPOSTOR, can_ai=True))))

    def test_an_unavailable_membership_check_denies_nobody(self):
        from app.services import instance_membership
        from fastapi import HTTPException

        async def unavailable(pubkey, force=False):
            raise HTTPException(503, "Instance profile verification is temporarily unavailable")

        with mock.patch.object(instance_membership, "status", unavailable):
            self.assertFalse(_run(nip05_access.is_member(MEMBER)))       # no extra grant …
            u = self._user(MEMBER, can_ai=True, can_image=True)
            self.assertTrue(_run(nip05_access.ai_allowed(u)))            # … and no denial either
            self.assertTrue(_run(self._cmd(u)._user_may("can_image")))

    def test_no_gate_writes_the_shared_blossom_whitelist(self):
        """The list is shared and hand-edited, and rewriting it from a partial read has already cost
        this project the whole thing once. Membership is resolved, never persisted."""
        def refuse(*a, **kw):
            raise AssertionError("a feature gate wrote a setting")

        with mock.patch.object(nip05_access.settings_store, "put", refuse), \
             mock.patch.object(nip05_access.settings_store, "put_many", refuse):
            self.assertTrue(_run(blossom_service.is_pubkey_allowed_async(self.db, MEMBER)))
            self.assertTrue(_run(nip05_access.ai_allowed(self._user(MEMBER))))
        self.assertEqual(self.settings["blossom_whitelist"], "")


class TestOneResolutionPoint(Base_):
    def test_all_four_gates_move_together(self):
        """The point of the module: one predicate, so the four gates cannot drift apart again."""
        u = self._user(MEMBER)
        svc = self._cmd(u)
        gates = {
            "ai": lambda: _run(nip05_access.ai_allowed(u)),
            "image": lambda: _run(svc._user_may("can_image")),
            "music": lambda: _run(svc._user_may("can_music")),
            "blossom": lambda: _run(blossom_service.is_pubkey_allowed_async(self.db, MEMBER)),
        }
        self.assertEqual({k: g() for k, g in gates.items()}, {k: True for k in gates})
        self.settings["nostr_relay_nip05_names"] = ""      # revoke the ONE grant
        nip05_access._granted_cache.update(raw=None, set=frozenset())
        self.assertEqual({k: g() for k, g in gates.items()}, {k: False for k in gates})


if __name__ == "__main__":
    unittest.main()
