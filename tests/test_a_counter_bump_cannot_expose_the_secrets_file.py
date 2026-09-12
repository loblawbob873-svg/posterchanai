"""Bumping a metric must not re-publish local_settings.json world-readable.

`local_settings.json` holds `nostr_relay_pg_dsn` — the relay database PASSWORD, for the store that
holds every user's events, encrypted DMs, notes, calendars and contacts. `_save_local_file()` was
hardened to publish it at 0600 after it was measured at -rw-r--r-- on a host with five other user
accounts.

`bump_counter()` was not. It also rewrites the WHOLE file, and it used a plain `open(tmp, "w")` —
0644 under a normal umask — which `os.replace` then carried onto the published file. So every
metric increment put the loose mode straight back, which is why a chmod by hand was measured back
at 0644 within seconds and why check_secrets_not_world_readable kept going red.

This drives the REAL functions against a temporary data dir and asserts the mode on disk.
"""
import json
import os
import stat
import tempfile
import unittest


class CounterBumpKeepsTheModeTight(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pc-settings-")
        self.path = os.path.join(self.dir, "local_settings.json")
        from app.services import settings_store as ss
        self.ss = ss
        self._orig = ss._LOCAL_PATH
        ss._LOCAL_PATH = self.path

    def tearDown(self):
        self.ss._LOCAL_PATH = self._orig

    def _mode(self):
        return stat.S_IMODE(os.stat(self.path).st_mode)

    def test_a_counter_bump_publishes_at_0600(self):
        """The regression, measured on disk rather than read out of the source."""
        # Seed the file the way a real node has it: secrets in, tight mode on.
        with open(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"nostr_relay_pg_dsn": "postgres://u:SECRET@h/db"}, f)
        self.assertEqual(self._mode(), 0o600, "fixture did not start tight")

        self.ss.bump_counter("pcai:metrics:test", "2026-09-12", "hits", 1)

        self.assertEqual(self._mode(), 0o600,
                         "a counter bump re-published the secrets file as %o" % self._mode())
        self.assertFalse(self._mode() & (stat.S_IRGRP | stat.S_IROTH),
                         "group or other can read the relay database password")

    def test_the_secret_survives_the_bump(self):
        """Tightening the mode must not be achieved by losing the file's contents."""
        with open(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump({"nostr_relay_pg_dsn": "postgres://u:SECRET@h/db"}, f)
        self.ss.bump_counter("pcai:metrics:test", "2026-09-12", "hits", 1)
        on_disk = json.load(open(self.path))
        self.assertEqual(on_disk.get("nostr_relay_pg_dsn"), "postgres://u:SECRET@h/db")
        self.assertIn("pcai:metrics:test", on_disk, "the counter was not actually written")

    def test_a_loose_file_is_TIGHTENED_not_preserved(self):
        """A node that is already 0644 must heal on the next write, not keep its mode."""
        with open(self.path, "w") as f:
            json.dump({"nostr_relay_pg_dsn": "postgres://u:SECRET@h/db"}, f)
        os.chmod(self.path, 0o644)
        self.ss.bump_counter("pcai:metrics:test", "2026-09-12", "hits", 1)
        self.assertEqual(self._mode(), 0o600,
                         "an already-loose file kept its mode (%o)" % self._mode())

    def test_both_writers_go_through_one_publisher(self):
        """Two copies of the same four lines is what failed; a third must not be possible."""
        import inspect
        src = inspect.getsource(self.ss)
        body = src[src.index("def _save_local_file"):]
        self.assertNotIn('with open(tmp, "w")', body,
                         "a writer publishes the secrets file without setting its mode")


if __name__ == "__main__":
    unittest.main()
