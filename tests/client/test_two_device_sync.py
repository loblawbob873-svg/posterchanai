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

    def test_a_file_too_big_to_hold_crosses_in_chunks(self):
        """The whole-file path holds the plaintext, the ciphertext and the upload body at once —
        three to four times the file — so a 2 GB document asked for ~7 GB and Chromium killed the
        renderer, which in the desktop app is a black window. The same ceiling is a proxy's: a
        request body over ~95 MB is refused by Cloudflare whatever the app allows. This asserts both
        halves: the bytes arrive identical, and no single request carried more than one chunk."""
        self.check("a-file-too-big-to-hold-crosses-in-chunks")

    def test_a_chunked_file_settles(self):
        """Found by writing it: `sha` has to keep meaning "the hash of this file's content". An
        earlier version stored the hash of the CHUNK LIST, which no scan will ever produce — so every
        sweep compared a whole-file hash against a list hash, called the file changed, and re-uploaded
        it. For ever, on both devices."""
        self.check("a-chunked-file-settles")

    def test_three_devices_converge(self):
        self.check("three-devices-converge")

    # ---- Files → Synced folders: a BROWSER editing a folder it holds none of ---------------------

    def test_web_delete_reaches_the_devices(self):
        """A file deleted from Files → Synced folders leaves every device — into .pc-trash, and it
        stays gone: a second round must not have anyone re-uploading it."""
        self.check("web-delete-reaches-the-devices")

    def test_a_web_delete_behaves_exactly_like_a_device_delete(self):
        """The risk in letting a browser write the manifest is that it becomes a SECOND way to
        delete, going round the engine and behaving subtly differently from the tested one. The same
        situation is run three ways — deleted on a device, deleted from the browser, and with the
        key dropped instead of tombstoned — and every observable outcome has to match.

        What the agreement-less device does is now decided by the CLOCK, and both answers are pinned:
        a copy older than the tombstone is not an edit and the deletion stands; a copy written after
        it is a real edit and still wins. This used to assert that all three shapes RESURRECT the
        file and called it engine policy — it was policy, and it was the bug that undid a whole
        fleet's delete (see test_a_device_that_lost_its_agreement_does_not_undo_a_delete). The
        removed-key arm still resurrects either way, because a dropped key leaves no timestamp to
        compare against, which is exactly why a delete is written as a tombstone."""
        self.check("a-web-delete-behaves-exactly-like-a-device-delete")

    def test_web_rename_carries_the_bytes(self):
        """A rename is a tombstone plus a new entry pointing at the SAME blob, so no bytes move:
        both devices end up with the new name, the same content, and not one new blob stored."""
        self.check("web-rename-carries-the-bytes")

    def test_web_rename_a_folder_moves_its_subtree(self):
        """Renaming a folder renames every path under it — and nothing that merely shares its
        prefix. `2025-summary.txt` is not in `2025/`."""
        self.check("web-rename-a-folder-moves-its-subtree")

    def test_web_upload_reaches_the_devices(self):
        """A file added from the browser is downloaded by every device, byte for byte, and nobody
        uploads over it afterwards."""
        self.check("web-upload-reaches-the-devices")


    # ---- the corrupted video, across two devices -------------------------------------------------

    def test_a_short_read_never_becomes_a_published_file(self):
        """THE ONE THAT SHIPPED. A real adapter hands back a partial buffer when it cannot fill a
        read (desktop `buf.subarray(0, bytesRead)`, Android `Arrays.copyOf(buf, got)`), and the
        uploader tested only `!plain.length` — catching an empty read and waving a partial one
        through: encrypted, uploaded, recorded, and `off` advanced by a WHOLE chunk, so the bytes in
        the gap were stored by nobody. Only files over one chunk take that path, which is exactly why
        images were fine and the videos would not open in anything, VLC included.

        Asserted as "nothing was PUBLISHED", not merely "it failed": the manifest entry is what
        reaches the other devices, so an upload that fails loudly and still writes one has lost the
        argument. Verified to bite — restore the old check and this reports
        `uploaded: [holiday.mp4], failed: [], entry: {chunks: […]}`, i.e. a corrupt file recorded as
        a success and handed to every device."""
        self.check("a-short-read-never-becomes-a-published-file")

    def test_an_interrupted_download_resumes_instead_of_restarting(self):
        """A network drop. Uploads have always resumed — a chunk is content-addressed and skipped
        when the server already holds it — but the receiving side walked the chunk list from the
        beginning every time, so a drop at 95% of an 8 GB video cost the whole 8 GB again, and on a
        link that drops regularly it may never finish.

        The resume is MEASURED (chunks actually fetched), not inferred from the file arriving: a full
        restart also produces a correct file, so an assertion on the bytes alone passes on exactly
        the behaviour this rules out. Verified — remove the resume and it reports 5 of 5 fetched."""
        self.check("an-interrupted-download-resumes-instead-of-restarting")

    def test_a_stale_part_file_is_not_resumed_onto(self):
        """The control that makes the resume safe. A part file left by a DIFFERENT version of the
        same path must not be spliced onto — the checksum catches it and the part is DISCARDED, so
        the retry starts clean. Without that pairing, resume turns one bad interruption into a file
        that can never download again."""
        self.check("a-stale-part-file-is-not-resumed-onto")

    def test_a_download_that_fails_its_checksum_never_lands(self):
        """"We need to verify that files are the same." Until this, a download was recorded as agreed
        carrying the REMOTE's csum without anybody hashing what actually landed — right length, wrong
        bytes, and `base` then asserted the file was correct for ever. Nothing would look at it again
        short of a Deep check, which is how a corrupt copy outlives the bug that made it.

        The tampering keeps the LENGTH, so the size check added alongside it cannot see this; only
        the checksum can. And the verification runs on the `.part` file BEFORE the rename, so the
        good copy already on disk survives untouched — after writeCommit it would be a report rather
        than a defence."""
        self.check("a-download-that-fails-its-checksum-never-lands")

    def test_the_same_video_crosses_intact_when_the_reads_are_whole(self):
        """The control: the identical file with a healthy adapter arrives byte for byte, so the test
        above is measuring the short read and not merely a file that never syncs."""
        self.check("the-same-video-crosses-intact-when-the-reads-are-whole")

    # ---- deleting a folder, across a whole fleet -------------------------------------------------
    #
    # Reported: "I deleted everything in Windows Explorer on Desktop, PosterChan says 1 file left in
    # Blossom for Pictures (desktop.ini), and the Laptop, Phone and Tablet never deleted the
    # pictures." Three tests, because the incident had three separable halves and only one of them
    # was the bug.

    def test_a_delete_reaches_every_other_device(self):
        """The baseline, and it always passed: with an intact agreement a delete on one machine is
        carried out on all the others. Kept so that a fix aimed at the case below cannot quietly
        break the case that worked."""
        self.check("a-delete-on-one-machine-reaches-the-whole-fleet")

    def test_a_device_that_lost_its_agreement_does_not_undo_a_delete(self):
        """THE BUG. A device with no `base` — reinstall, an app update that moved the storage origin,
        "Stop syncing" and back — read "I still have this file" as "I edited this file", so
        diff()'s delete-loses-to-edit arm made it UPLOAD every picture back over its tombstone. The
        machine that did the deleting then downloaded them all again. Measured before the fix:
        trashed 0, uploaded [a,b,c], manifest back to four live entries."""
        self.check("a-device-that-lost-its-agreement-does-not-resurrect-a-delete")

    def test_a_file_put_back_after_a_delete_is_not_destroyed(self):
        """THE CONTROL, and the one that matters most. The fix above is a TIEBREAK, not "a tombstone
        always wins" — a copy genuinely written after the deletion is a real edit and must survive
        and republish. If this ever fails, the fix has become silent data loss for every device that
        loses its agreement, which is worse than the bug it replaced."""
        self.check("a-file-written-after-the-delete-still-wins")


    def test_a_backlog_does_not_starve_deletions(self):
        """Deletions used to run after every transfer, so on a folder with a backlog they were never
        reached — and an interrupted sweep restarted the transfer loops from the top. Pressing Sync
        appeared to do nothing at all."""
        self.check("a-backlog-does-not-starve-deletions")

    def test_two_devices_converge_and_stop(self):
        """THE ONE THAT ASKS WHAT A USER ASKS. Every other scenario pins one rule, and every one of
        them passed while the feature was failing in the field — a per-rule test can only catch a
        rule that is wrong, never a system that never settles. This runs an ordinary messy state
        (edits and deletes on both sides, the same bytes added independently, new files on each) and
        asserts the folder ends up IDENTICAL on both machines and then goes quiet.

        It found a real one immediately: `same()` compares an absent local file against a tombstone
        in `base` as CHANGED, so every already-deleted path re-proposed its own deletion on every
        sweep, for ever — a manifest with 3,930 tombstones rewriting and re-uploading itself on a
        loop while logging an unchanged count, which is why it looked healthy in production."""
        self.check("two-devices-converge-and-stop")

    def test_a_settled_folder_does_nothing_for_ever(self):
        """Convergence is only half of it; staying converged is the other half. A folder that keeps
        moving files after it has agreed is the infinite resync this feature has hit repeatedly, each
        time from a different cause and each time looking to the user like a sync that never ends."""
        self.check("a-settled-folder-does-nothing-for-ever")

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
