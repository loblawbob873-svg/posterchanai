"""The fediverse bridge must never conclude "switched off" from an UNHYDRATED settings cache.

settings_store.get() falls back to the literal default when the cache hasn't synced with the relay,
so `fedi_bridge_enabled` reads "false" on a store holding nothing — indistinguishable from an admin
turning it off. The 5-minute resubscribe cycle acted on exactly that: a re-hydrate returned 0
settings, the cycle woke that second, read the default, closed the socket and returned. The bridge
sat silently dead — no error, no rejection line — and every note posted afterwards had nothing
listening. is_hydrated() is the difference between "off" and "don't know yet".
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class BridgeKillSwitch(unittest.TestCase):
    def setUp(self):
        from app.services import settings_store
        self.ss = settings_store
        self._hyd = settings_store._HYDRATED

    def tearDown(self):
        self.ss._HYDRATED = self._hyd

    def test_unhydrated_is_not_off(self):
        from app.services import fedi_nostr_writeback_service as wb
        self.ss._HYDRATED = False
        self.assertTrue(wb._bridge_on(),
                        "an unhydrated cache means 'don't know yet' — tearing the subscription down "
                        "on it kills the bridge silently until the next restart")

    def test_a_real_off_still_stops_it(self):
        from app.services import fedi_nostr_writeback_service as wb
        self.ss._HYDRATED = True
        self.ss.put("fedi_bridge_enabled", "false", write_relay=False)
        try:
            self.assertFalse(wb._bridge_on(), "the kill-switch must still work when we DO know")
        finally:
            self.ss.put("fedi_bridge_enabled", "true", write_relay=False)

    def test_no_raw_default_reads_left(self):
        """Any reintroduced `get("fedi_bridge_enabled", "false")` in the control path is the bug."""
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "app/services/fedi_nostr_writeback_service.py"), encoding="utf-8") as f:
            src = f.read()
        body = src.split("def _bridge_on", 1)[1].split("\n\n\n", 1)[1]   # everything AFTER the helper
        self.assertNotIn('settings_store.get("fedi_bridge_enabled"', body,
                         "control-path reads must go through _bridge_on()")


if __name__ == "__main__":
    unittest.main(verbosity=1)
