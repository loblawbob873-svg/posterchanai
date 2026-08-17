"""The folder-sync store at real folder sizes — the ceiling that made a big folder unsyncable.

Run: venv-unified/bin/python -m unittest tests.client.test_sync_store_scale

Reported from use, on a folder of 15790 files: "It started to resync all 15790 files from the
beginning. Sync finished the first sync and immediately started syncing everything all over again."

Both halves of that are one cause. NIP-44 refuses a plaintext over 65535 bytes and a manifest entry
measures ~174, so the document could hold about 376 files. Past that `store.save()` threw at the very
last step of every sweep: the uploads had all happened, the manifest was never stored, `base` was
never written, and the next sweep read the whole folder as new. Everything except the final step
worked, which is exactly why it looked like a working sync.

`base` had a second, independent version of the same failure: it went to localStorage inside a
try/catch that swallowed everything, including the quota error a 2.6 MB agreement earns.

`sync_store_sim.js` loads the SHIPPED sync.js against stub globals that keep the real limits — the
NIP-44 ceiling, a 5 MB localStorage quota — and drives `PCSync.store` at those sizes.
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

    def test_a_folder_past_the_nip44_ceiling_saves(self):
        """The one this file exists for. Past ~45 KB the paths move to an encrypted Blossom blob,
        the same thing the files index does for the same reason."""
        self.check("a-folder-past-the-nip44-ceiling-saves")

    def test_a_huge_manifest_round_trips(self):
        self.check("a-huge-manifest-round-trips")

    def test_an_old_client_cannot_read_a_v2_manifest_as_empty(self):
        """A build older than this change looks for `sealed` and falls back to `doc.paths`, so a v2
        document would read as an EMPTY manifest — every file 'deleted elsewhere', trashed on that
        device and tombstoned for the others. `sealed` therefore carries a marker that cannot
        decrypt, so an old client fails loudly instead."""
        self.check("an-old-client-cannot-read-a-v2-manifest-as-empty")

    def test_a_huge_base_persists(self):
        """~2.6 MB of agreement, against a 5 MB localStorage budget shared with everything else."""
        self.check("a-huge-base-persists")

    def test_a_base_that_cannot_be_stored_throws(self):
        """A silent failure here is an infinite resync whose only symptom is the progress counter
        starting from one again."""
        self.check("a-base-that-cannot-be-stored-throws")

    def test_an_existing_localstorage_base_is_still_read(self):
        """Or every device that already had an agreement re-uploads its whole folder once."""
        self.check("an-existing-localstorage-base-is-still-read")

    def test_a_cached_manifest_is_not_shared_between_callers(self):
        """The decrypted paths are cached by the pointer they came from, because a sweep re-reads the
        manifest about twenty times and decrypting three megabytes each time is most of what a
        checkpoint costs. A cache that handed out the same object would be worse than the cost it
        saves: a sweep mutates what it is given."""
        self.check("a-cached-manifest-is-not-shared-between-callers")

    def test_a_small_folder_stays_inline(self):
        self.check("a-small-folder-stays-inline")

    def test_the_collapse_guard_still_gets_a_count(self):
        """`n` is the only thing the server can see, and it is what stands between a bug and a wiped
        folder — so it has to stay truthful when the paths move into a blob."""
        self.check("the-collapse-guard-still-gets-a-count")

    def test_our_own_document_is_restored_from_the_journal(self):
        """THE COLLAPSE GUARD IS GONE, AND ITS ABSENCE IS THE POINT.

        It existed because one document had many writers: a device with a stale copy could write it
        back and erase what another had added, and a server refusing a sharp shrink was the only
        thing between that and an emptied folder. A document with ONE writer cannot have that
        problem — a shrink in this device's own record is this device's own doing.

        What replaces it is this: our view is rebuilt from our journal on every sweep, so a document
        that is somehow lost or emptied comes back without re-uploading a byte."""
        self.check("our own document is restored from the journal, not from the network")

    def test_each_save_points_at_a_fresh_blob(self):
        """The server keeps one generation of manifest blob and releases the one behind it; that is
        only possible if each save is identifiable."""
        self.check("each-save-points-at-a-fresh-blob")


if __name__ == "__main__":
    unittest.main()
