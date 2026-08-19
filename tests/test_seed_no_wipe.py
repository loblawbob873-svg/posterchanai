"""Regression tests for the 2026-06-23 settings wipe (app/services/settings_store.py).

Run: venv/bin/python -m unittest tests.test_seed_no_wipe

The wipe: seed_relay_defaults read "what the relay already has" over the relay WebSocket, which under
startup load returned a PARTIAL set, so ~119 real settings looked "missing" and got overwritten by
defaults (with fresh timestamps, so they won — the LLM/bots/LB all broke). These tests pin the fix:
seeding now reads existing keys via a RACE-FREE direct DB path and REFUSES to seed when that read
isn't authoritative — so a transient short read can never again clobber the durable Nostr store.
"""
import asyncio
import unittest
from unittest import mock

from app.services import settings_store as S


def _run(coro):
    return asyncio.run(coro)


class TestSeedFailSafe(unittest.TestCase):
    def setUp(self):
        # write_through is the only thing that touches the relay; spy on it so a test "seed" is visible
        self.wt = mock.AsyncMock(return_value=0)
        self.p_wt = mock.patch.object(S, "write_through", self.wt)
        self.p_set = mock.patch.object(S, "_set_local")
        # THE OPERATOR KEY, PINNED — it is a precondition here, not the thing under test.
        #
        # `seed_relay_defaults` opens with `op_sk = _OP_SK or _operator_seckey(db)` and returns 0
        # when that is empty. `_OP_SK` is a PROCESS-GLOBAL, so whether these tests found a key
        # depended entirely on what had run before them in the same interpreter: alone they passed,
        # in a full run they returned 0 and the spy was never awaited, and the failure surfaced as
        # `NoneType has no attribute 'args'` — which reads like a broken assertion rather than a
        # test that never reached the code it is about. Three of them sat red for exactly that.
        #
        # These tests exist to pin the SEEDING DECISION (what is authoritative, what may be
        # overwritten). Operator-key discovery is a different mechanism with its own tests, so it is
        # held still here rather than left to whatever the process happens to be carrying.
        self.p_sk = mock.patch.object(S, "_OP_SK", b"\x01" * 32)
        self.p_op = mock.patch.object(S, "_operator_seckey", return_value=b"\x01" * 32)
        self.p_sk.start(); self.p_op.start()
        self.addCleanup(self.p_sk.stop); self.addCleanup(self.p_op.stop)
        self.p_wt.start(); self.p_set.start()
        self.addCleanup(self.p_wt.stop); self.addCleanup(self.p_set.stop)

    def _seed(self, defaults):
        return _run(S.seed_relay_defaults(db=None, defaults=defaults))

    def test_established_node_seeds_nothing(self):
        # the relay already holds these keys (direct read sees them) → seed must NOT touch them
        with mock.patch.object(S, "_relay_setting_keys_from_db",
                               return_value=({"llm_model_path", "chat_server_urls"}, True)):
            n = self._seed({"llm_model_path": "/default.gguf", "chat_server_urls": ""})
        self.assertEqual(n, 0)
        self.wt.assert_not_awaited()        # the bug was this firing on an established node

    def test_partial_read_does_not_reseed_existing_key(self):
        # even if the direct read only returns SOME keys, a key it DID return is never re-seeded
        with mock.patch.object(S, "_relay_setting_keys_from_db",
                               return_value=({"chat_server_urls"}, True)):
            self.wt.return_value = 1
            n = self._seed({"chat_server_urls": "", "brand_new_key": "x"})
        # only the genuinely-absent new key may be seeded; the existing one must be left alone
        self.wt.assert_awaited_once()
        seeded = self.wt.await_args.args[1]
        self.assertNotIn("chat_server_urls", seeded)
        self.assertIn("brand_new_key", seeded)

    def test_unauthoritative_read_refuses_to_seed(self):
        # the actual wipe trigger: read came back short/untrusted → seed must write NOTHING
        with mock.patch.object(S, "_relay_setting_keys_from_db", return_value=(set(), False)):
            n = self._seed({"a": "1", "b": "2", "c": "3"})
        self.assertEqual(n, 0)
        self.wt.assert_not_awaited()

    def test_genuine_fresh_node_seeds_all(self):
        # empty + authoritative (relay event tables not created yet) → real first boot, seed everything
        with mock.patch.object(S, "_relay_setting_keys_from_db", return_value=(set(), True)):
            self.wt.return_value = 2
            self._seed({"a": "1", "b": "2"})
        seeded = self.wt.await_args.args[1]
        self.assertEqual(set(seeded), {"a", "b"})

    def test_local_only_keys_are_never_seeded_to_relay(self):
        with mock.patch.object(S, "_relay_setting_keys_from_db", return_value=(set(), True)), \
             mock.patch.object(S, "_is_local_only", side_effect=lambda k: k == "plumbing"):
            self.wt.return_value = 1
            self._seed({"plumbing": "x", "real": "y"})
        seeded = self.wt.await_args.args[1]
        self.assertNotIn("plumbing", seeded)
        self.assertIn("real", seeded)


class TestDirectKeyReadGuards(unittest.TestCase):
    def test_no_operator_key_is_not_authoritative(self):
        with mock.patch.object(S, "_OP_SK", None), \
             mock.patch.object(S, "_operator_seckey", return_value=None):
            keys, ok = S._relay_setting_keys_from_db(db=None)
        self.assertEqual(keys, set())
        self.assertFalse(ok)                # no key → cannot trust → do not seed

    def test_missing_event_tables_is_fresh_node_authoritative(self):
        db = mock.Mock()
        db.execute.side_effect = Exception('relation "events" does not exist')
        with mock.patch.object(S, "_OP_SK", b"\x01" * 32), \
             mock.patch("app.services.nostr.nostr_service.derive_pubkey", return_value="ab" * 32):
            keys, ok = S._relay_setting_keys_from_db(db=db)
        self.assertEqual(keys, set())
        self.assertTrue(ok)                 # fresh node: empty but trustworthy → first-boot seed is OK

    def test_other_db_error_is_not_authoritative(self):
        db = mock.Mock()
        db.execute.side_effect = Exception("connection reset")
        with mock.patch.object(S, "_OP_SK", b"\x01" * 32), \
             mock.patch("app.services.nostr.nostr_service.derive_pubkey", return_value="ab" * 32):
            keys, ok = S._relay_setting_keys_from_db(db=db)
        self.assertFalse(ok)                # unknown failure → refuse to seed


if __name__ == "__main__":
    unittest.main()
