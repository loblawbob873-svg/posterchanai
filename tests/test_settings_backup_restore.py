"""Tests for settings disaster-recovery (app/services/settings_backup.py).

Run: venv/bin/python -m unittest tests.test_settings_backup_restore

These lock down the DR semantics that recovered the node after a relay wipe (settings silently
replaced by defaults → LLM/bots/LB down): a JSON backup round-trips losslessly, "fill" repairs a
wipe WITHOUT clobbering post-backup edits, "overwrite" is a full rollback, and a verify pass proves
the relay matches the backup. The relay I/O is mocked, so the tests are fast and need no relay/DB.
"""
import asyncio
import unittest
from unittest import mock

from app.services import settings_backup as B


def _run(coro):
    return asyncio.run(coro)


class TestSerializeRoundTrip(unittest.TestCase):
    def test_serialize_then_deserialize_is_identity(self):
        s = {"chat_server_urls": "192.168.0.2, 192.168.0.85", "blossom_blob_ttl_days": "365",
             "smtp_password": "eGa8wjntydAu0P5UhmFr", "blank": ""}
        self.assertEqual(B.deserialize(B.serialize(s)), s)

    def test_values_coerced_to_strings(self):
        # settings are always strings on the wire; None → "" so a backup never carries a null
        blob = B.serialize({"a": 365, "b": None})
        self.assertEqual(B.deserialize(blob), {"a": "365", "b": ""})

    def test_envelope_has_format_and_count(self):
        import json
        env = json.loads(B.serialize({"a": "1", "b": "2"}, node="server1"))
        self.assertEqual(env["format"], "posterchanai.settings-backup/1")
        self.assertEqual(env["count"], 2)
        self.assertEqual(env["node"], "server1")

    def test_deserialize_tolerates_bare_dict(self):
        self.assertEqual(B.deserialize('{"a": "1"}'), {"a": "1"})

    def test_deserialize_rejects_non_object(self):
        with self.assertRaises(ValueError):
            B.deserialize("[1, 2, 3]")


class TestPlanRestoreFill(unittest.TestCase):
    """'fill' = repair a wipe: only touch keys that are missing/empty now but real in the backup."""

    def test_fills_wiped_blank_key(self):
        current = {"chat_server_urls": ""}                       # wiped to blank
        backup = {"chat_server_urls": "192.168.0.2, 192.168.0.85"}
        self.assertEqual(B.plan_restore(current, backup, mode="fill"),
                         {"chat_server_urls": "192.168.0.2, 192.168.0.85"})

    def test_fills_missing_key(self):
        self.assertEqual(B.plan_restore({}, {"storage_server_url": "http://192.168.0.85:3051"},
                                        mode="fill"),
                         {"storage_server_url": "http://192.168.0.85:3051"})

    def test_does_not_clobber_a_value_changed_after_backup(self):
        # the model path you fixed in Admin AFTER the backup must survive a fill-restore
        current = {"llm_model_path": "/good/Qwen.gguf"}
        backup = {"llm_model_path": "/old/model.gguf"}
        self.assertEqual(B.plan_restore(current, backup, mode="fill"), {})

    def test_does_not_write_blank_over_present(self):
        # a backup that itself recorded a blank must not erase a currently-set value
        self.assertEqual(B.plan_restore({"k": "v"}, {"k": ""}, mode="fill"), {})

    def test_returns_empty_when_in_sync(self):
        self.assertEqual(B.plan_restore({"k": "v"}, {"k": "v"}, mode="fill"), {})


class TestPlanRestoreOverwrite(unittest.TestCase):
    """'overwrite' = full rollback to the snapshot."""

    def test_overwrites_every_differing_key(self):
        current = {"a": "new", "b": "same", "c": ""}
        backup = {"a": "old", "b": "same", "c": "real"}
        self.assertEqual(B.plan_restore(current, backup, mode="overwrite"),
                         {"a": "old", "c": "real"})

    def test_never_invents_keys_absent_from_backup(self):
        # a restore can repair/rollback but never delete: keys only-in-current are left alone
        self.assertEqual(B.plan_restore({"only_here": "x"}, {}, mode="overwrite"), {})

    def test_rejects_unknown_mode(self):
        with self.assertRaises(ValueError):
            B.plan_restore({}, {}, mode="bogus")


class TestVerifyRestore(unittest.TestCase):
    def test_clean_when_all_match(self):
        self.assertEqual(B.verify_restore({"a": "1", "b": "2"}, {"a": "1", "b": "2"}, ["a", "b"]), [])

    def test_reports_mismatch_tuples(self):
        bad = B.verify_restore({"a": "wrong"}, {"a": "right"}, ["a"])
        self.assertEqual(bad, [("a", "right", "wrong")])

    def test_treats_missing_as_mismatch(self):
        bad = B.verify_restore({}, {"a": "right"}, ["a"])
        self.assertEqual(bad, [("a", "right", None)])


class TestEndToEndIncidentScenario(unittest.TestCase):
    """Reproduce today's incident against a fake relay: snapshot → wipe → restore → verify."""

    def _fake_relay(self, initial):
        store = dict(initial)

        async def snapshot(db):
            return dict(store)

        async def apply_restore(db, payload):
            store.update(payload)            # write_through with a fresh timestamp wins
            return len(payload)

        return store, snapshot, apply_restore

    def test_fill_restore_recovers_wiped_and_preserves_new(self):
        good = {
            "llm_model_path": "/models/Qwen3.5-9B-abliterated-Q4_K_M.gguf",
            "chat_server_urls": "192.168.0.2, 192.168.0.85",
            "blossom_public_url": "https://media.poster.place",
            "smtp_host": "blizzard.mxrouting.net",
        }
        backup_blob = B.serialize(good)

        # the wipe: shareable keys reset to defaults/blank, but the operator already re-fixed the
        # model path in Admin (a value that is NEWER than the backup and must NOT be rolled back)
        wiped = {
            "llm_model_path": "/models/Qwen3.5-9B-abliterated-Q4_K_M.gguf",  # already correct, newer
            "chat_server_urls": "",
            "blossom_public_url": "",
            "smtp_host": "",
        }
        store, snapshot, apply_restore = self._fake_relay(wiped)

        async def drive():
            backup = B.deserialize(backup_blob)
            current = await snapshot(None)
            payload = B.plan_restore(current, backup, mode="fill")
            await apply_restore(None, payload)
            after = await snapshot(None)
            return payload, B.verify_restore(after, backup, payload.keys())

        payload, bad = _run(drive())
        self.assertEqual(set(payload), {"chat_server_urls", "blossom_public_url", "smtp_host"})
        self.assertEqual(bad, [])                                   # verify clean
        self.assertEqual(store["chat_server_urls"], "192.168.0.2, 192.168.0.85")
        self.assertEqual(store["blossom_public_url"], "https://media.poster.place")

    def test_apply_restore_calls_write_through_and_seeds_cache(self):
        # the real apply_restore must both update the in-process cache and write through to the relay
        with mock.patch("app.services.settings_store._set_local") as set_local, \
             mock.patch("app.services.settings_store.ensure_operator_key", return_value=True), \
             mock.patch("app.services.settings_store.write_through",
                        new=mock.AsyncMock(return_value=2)) as wt:
            n = _run(B.apply_restore(None, {"a": "1", "b": "2"}))
        self.assertEqual(n, 2)
        wt.assert_awaited_once()
        self.assertEqual(set_local.call_count, 2)                  # both keys reflected into cache


if __name__ == "__main__":
    unittest.main()
