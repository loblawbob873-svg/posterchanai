"""Folder sync between TWO devices — the situation this feature has never actually been in.

Run: venv-unified/bin/python -m unittest tests.client.test_two_device_sync

test_folder_sync.py drives the engine with a hand-written "remote" snapshot and test_sync_run.py
drives the executor with an injected store. Both passed for weeks while cross-device sync could not
work at all: the manifest was keyed on the PLATFORM's directory handle, so every device wrote and
read a different document and synced only with itself. A test that supplies the shared snapshot by
hand is the one shape of test that cannot see that — it hands the devices the very thing the bug
stops them from sharing.

`two_device_sim.js` runs two (and in one case three) independent devices — separate in-memory
filesystems, separate `base`, separate platform ids — against ONE manifest store addressed exactly
the way the server addresses it, both running the shipped foldersync.js and syncrun.js.

Verified to bite: reverting syncrun.js to key the manifest on `o.id` fails nine of the twelve
scenarios below, and leaves `keyed-by-platform-id-cannot-pair` passing — which is what that scenario
is for.

WHAT THIS DOES NOT PROVE. The platform adapters (Electron's fsbridge, Android's SAF plugin), the
NIP-44 sealing and the Blossom round trip are all stubbed here. A real desktop → phone trip is still
the only thing that exercises those. What this removes from the list of suspects is the engine, the
ordering and the keying — which is where every bug in this feature has been so far.
"""

import json
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SIM = os.path.join(HERE, "two_device_sim.js")
SYNC_JS = os.path.join(REPO, "static", "js", "client", "sync.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class TestTwoDeviceSync(unittest.TestCase):
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

    def test_a_pairs_by_name(self):
        """The one this file exists for: two platform handles, one name, files actually cross."""
        self.check("pairs-by-name")

    def test_a_platform_id_keying_is_still_broken(self):
        """The control. Keyed on the device-local handle the same scenario transfers nothing — if
        this ever starts passing files across, the sim has stopped being able to see the bug."""
        self.check("keyed-by-platform-id-cannot-pair")

    def test_edit_comes_back(self):
        self.check("edit-comes-back")

    def test_delete_propagates_to_trash(self):
        """Deleted on one device, gone on the other — into .pc-trash, never destroyed."""
        self.check("delete-propagates-to-trash")

    def test_conflict_keeps_both(self):
        self.check("conflict-keeps-both")

    def test_exclude_here_does_not_delete_there(self):
        """53d0dd40, re-checked with two real devices instead of a synthetic remote snapshot."""
        self.check("exclude-here-does-not-delete-there")

    def test_idempotent(self):
        """Ten further sweeps move nothing. Growth here is the re-upload loop."""
        self.check("idempotent")

    def test_same_bytes_both_sides_is_not_a_conflict(self):
        self.check("same-bytes-both-sides-is-not-a-conflict")

    def test_incremental_sweep_round_trips(self):
        """The ordinary on-battery sweep never hashes, so size+mtime is the only comparison."""
        self.check("incremental-sweep-round-trips")

    def test_stale_base_is_what_deletes_everything(self):
        """Why 'Stop syncing' must clear `base` under the key it was WRITTEN under."""
        self.check("stale-base-is-what-deletes-everything")

    def test_a_missing_blob_does_not_poison_the_sweep(self):
        self.check("a-missing-blob-does-not-poison-the-sweep")

    def test_an_interrupted_sweep_resumes(self):
        """`base` advanced per file in MEMORY and was written once, at the end — so a sweep that was
        killed recorded nothing and the next one started at the first file. Reported from a real
        15790-file folder: 'the windows app didn't finish it, it started to resync from the
        beginning'."""
        self.check("an-interrupted-sweep-resumes")

    def test_an_empty_base_does_not_conflict_the_whole_folder(self):
        """An ordinary sweep does not hash, so a convergence test that required both shas could
        never fire on one — and with an empty base every path looked divergent. Reverting the fix
        duplicates the entire folder as '(conflict from …)' copies: 40 files became 80."""
        self.check("an-empty-base-does-not-conflict-the-whole-folder")

    def test_three_devices_converge(self):
        self.check("three-devices-converge")


@unittest.skipIf(not NODE, "no node on this node")
class TestPairKeyCrossesTheWire(unittest.TestCase):
    """The client sanitises the folder name and the SERVER sanitises it again into a d-tag. Those two
    have to agree character for character, because the d-tag is the address: a name the client keeps
    and the server strips (or the other way round) sends two devices to two different documents, and
    they sync happily with themselves — the exact failure the pair key was introduced to fix, moved
    one layer down where no amount of two-device testing in JS would find it.
    """

    NAMES = [
        "Documents", "Pictures", "my-photos", "work_files",
        "Mes Documents",        # a space
        "École",                # non-ASCII
        "Documents/2026",       # a separator — must never survive into the d-tag
        "..",                   # traversal
        "pcai:note:1",          # a colon: could address ANOTHER of the user's documents
        "a" * 80,               # over the length cap
        "abc",                  # under the 4-char floor
        "  Documents  ",
    ]

    def test_client_and_server_sanitise_identically(self):
        # sync.js is a browser IIFE hanging off `window`, so it is loaded as text and only its
        # pairKey() is lifted out — evaluating the whole module would need a DOM.
        js = """
          const fs = require('fs');
          const src = fs.readFileSync(%s, 'utf8');
          const m = src.match(/function pairKey\\(name\\)\\{[\\s\\S]*?\\n  \\}/);
          if(!m) throw new Error('pairKey() not found in sync.js — has it been renamed?');
          eval(m[0]);
          const out = {};
          for(const n of %s) out[n] = pairKey(n);
          process.stdout.write(JSON.stringify(out));
        """ % (json.dumps(SYNC_JS), json.dumps(self.NAMES))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, "node failed:\n" + r.stderr[-2000:])
        client = json.loads(r.stdout)

        import sys
        sys.path.insert(0, REPO)
        from app.routers.client import _sync_folder_key

        for name in self.NAMES:
            key = client[name]
            server = _sync_folder_key(key)
            if 4 <= len(key) <= 64:
                self.assertEqual(
                    server, "pcai:sync:" + key,
                    "the server rewrote the client's folder key %r → %r. The d-tag is the address: "
                    "two devices that sanitise differently address different documents and sync "
                    "only with themselves." % (key, server),
                )
            else:
                self.assertIsNone(
                    server,
                    "%r sanitises to %r, which the client would send and the server must refuse "
                    "rather than silently address something else" % (name, key),
                )

    def test_a_folder_name_can_never_address_another_document(self):
        """The d-tag namespace is SHARED — notes, calendars, contacts and the files index all live in
        kind 30078 under one key. A folder name carrying a colon or a wildcard that survived
        sanitisation could overwrite one of them."""
        import sys
        sys.path.insert(0, REPO)
        from app.routers.client import _sync_folder_key

        for evil in ["pcai:note:abcd", "../files-index", "*", "pcai:files-index", "a:b"]:
            key = _sync_folder_key(evil)
            self.assertTrue(key is None or key.startswith("pcai:sync:"), evil)
            if key:
                self.assertEqual(key.count(":"), 2, "a folder key escaped its namespace: %r" % key)


if __name__ == "__main__":
    unittest.main()
