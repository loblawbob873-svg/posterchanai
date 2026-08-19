"""Puts sync_store_sim.js — the transport layer's suite — INTO the suite.

The store under test is the SHIPPED sync.js, loaded against stub globals and a fake
/client/sync-state that enforces the real endpoint's rules (per-file CAS, the era, delta reads).
Each scenario is a rule that once cost a folder: the old per-document ceiling that made a
15,790-file folder unsaveable, the stale write that used to be a silent overwrite, the re-add
ghosts the era exists to kill, and a journal whose failure to persist was an infinite resync.
"""
import json
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SIM = os.path.join(HERE, "sync_store_sim.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class TestSyncStoreScale(unittest.TestCase):
    _rows = None

    @classmethod
    def setUpClass(cls):
        r = subprocess.run([NODE, SIM], capture_output=True, text=True, timeout=300)
        if not r.stdout.strip():
            raise AssertionError("the simulation produced nothing:\n" + r.stderr[-2000:])
        try:
            rows = json.loads(r.stdout)
        except json.JSONDecodeError:
            raise AssertionError("simulation crashed:\n" + r.stdout[-1500:] + "\n" + r.stderr[-1500:])
        cls._rows = {row["name"]: row for row in rows}

    def check(self, name):
        self.assertIn(name, self._rows,
                      "scenario missing from the simulation: %r (have %s)" % (name, list(self._rows)))
        row = self._rows[name]
        self.assertTrue(row["ok"], "%s: %s" % (name, json.dumps(row["detail"], indent=1)))

    def test_a_huge_folder_round_trips(self):
        """2,000 per-file records — five times the OLD per-document ceiling — all land and all read
        back. The ceiling that once made a real folder unsaveable is per file now, where it never
        binds."""
        self.check("a-huge-folder-round-trips")

    def test_an_oversized_chunk_list_is_sealed_and_round_trips(self):
        """An Android-chunked file past ~4 GB lists more chunks than NIP-44 will seal: the list
        moves into its own encrypted blob and the record carries the pointer. The old shape's
        ceiling failed the whole folder at the last step, silently, for ever."""
        self.check("an-oversized-chunk-list-is-sealed-and-round-trips")

    def test_a_stale_write_is_refused_and_named(self):
        """The compare-and-swap: the loser of a race is told, keeps its bytes, and nothing is
        silently overwritten."""
        self.check("a-stale-write-is-refused-and-named")

    def test_an_era_shift_voids_the_journal(self):
        """Remove-and-re-add cannot haunt: a journal from the dead world is cleared before the
        executor can read it — the 373-ghost-conflicts phone, made impossible."""
        self.check("an-era-shift-voids-the-journal")

    def test_a_delta_read_fetches_only_the_news(self):
        """A 12,000-file folder costs one full read ever; every later look asks `since`."""
        self.check("a-delta-read-fetches-only-the-news")

    def test_tombstones_travel_with_their_addresses(self):
        """A deletion is a positive record that keeps the file's address — the account-wide Restore
        depends on both halves."""
        self.check("tombstones-travel-with-their-addresses")

    def test_a_checksum_flag_rides_the_record(self):
        """The bad-copy repair: the puller's refusal reaches the holder on the file's own record."""
        self.check("a-checksum-flag-rides-the-record")

    def test_a_huge_base_persists(self):
        """~2.6 MB of journal, against a 5 MB localStorage budget — which is why it is IndexedDB."""
        self.check("a-huge-base-persists")

    def test_a_base_that_cannot_be_stored_throws(self):
        """A silent failure here is an infinite resync whose only symptom is the progress counter
        starting from one again."""
        self.check("a-base-that-cannot-be-stored-throws")

    def test_an_existing_localstorage_base_is_still_read(self):
        """Or every device that already had an agreement re-uploads its whole folder once."""
        self.check("an-existing-localstorage-base-is-still-read")


class TestChunkCeiling(unittest.TestCase):
    """The sweep must clamp its chunk to what the NODE accepts — a chunk IS one upload."""

    def test_the_sweep_asks_for_the_clamped_size(self):
        src = open(os.path.join(HERE, "..", "..", "static", "js", "client", "sync.js"),
                   encoding="utf-8").read()
        self.assertIn("chunkBytes: await chunkSize()", src,
                      "the sweep no longer clamps its chunk to the node's upload limit")


if __name__ == "__main__":
    unittest.main()
