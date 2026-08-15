"""The folder-sync decision engine, run under node against the shipped module.

Run: venv-unified/bin/python -m unittest tests.client.test_folder_sync

static/js/client/foldersync.js decides whether your Documents get deleted, so it is a pure function
of three snapshots — local, remote, and what this device last agreed with — and every case below is
a scenario a two-way sync gets wrong if it keeps fewer than three.

The cases are chosen as the ways real sync products have eaten real files:

  * A file present here and absent there is AMBIGUOUS with two snapshots. It is either new here or
    deleted there, and a guess is either "everything you delete comes back" or "everything you add
    gets deleted". `base` is what disambiguates it, per path.
  * Both sides edited → never pick a winner. Arbitrary bytes have no merge. Keep both.
  * Delete vs edit → the edit wins, in both directions. Resurrecting a file costs one more delete;
    the other way costs the file.
  * The same bytes arriving on both sides independently (a photo library seeded from one camera)
    must NOT produce thousands of conflict copies on first sync.
  * mtime slop: FAT32/exFAT/SMB/Android SAF round timestamps. Without a tolerance, every file on a
    removable drive reports as changed on every sweep, forever.
"""
import json
import os
import shutil
import subprocess
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = os.path.join(REPO, "static", "js", "client", "foldersync.js")
NODE = shutil.which("node") or shutil.which("nodejs")

DAY = 86400000


def f(csum, size=10, mtime=1000):
    """An entry identified by CONTENT.

    `csum` is the file's own hash. A manifest entry's `sha` is something else entirely — the address
    of its encrypted blob, the hash of the CIPHERTEXT — and the two were once the same field, so as
    soon as a device hashed anything it compared one against the other, never matched, and duplicated
    every identical file as a conflict copy.
    """
    return {"csum": csum, "size": size, "mtime": mtime}


@unittest.skipIf(not NODE, "no node on this node")
class TestFolderSync(unittest.TestCase):
    def plan(self, local, remote, base, device="laptop", now=5 * DAY):
        js = (
            "const S=require(%s);"
            "const out=S.diff({local:%s, remote:%s, base:%s, device:%s, now:%d});"
            "process.stdout.write(JSON.stringify(out));"
        ) % (json.dumps(MOD), json.dumps(local), json.dumps(remote),
             json.dumps(base), json.dumps(device), now)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-2000:])
        return json.loads(r.stdout)

    def paths(self, plan, key):
        return sorted(a["path"] for a in plan[key])

    # ---- the ambiguity that needs three snapshots ------------------------------------------

    def test_new_here_is_uploaded_not_deleted(self):
        p = self.plan(local={"a.txt": f("A")}, remote={}, base={})
        self.assertEqual(self.paths(p, "upload"), ["a.txt"])
        self.assertEqual(p["deleteLocal"], [])

    def test_deleted_there_is_deleted_here_not_re_uploaded(self):
        """Same local/remote shape as the case above — only `base` tells them apart."""
        p = self.plan(local={"a.txt": f("A")}, remote={}, base={"a.txt": f("A")})
        self.assertEqual(self.paths(p, "deleteLocal"), ["a.txt"])
        self.assertEqual(p["upload"], [])

    def test_new_elsewhere_is_downloaded(self):
        p = self.plan(local={}, remote={"b.txt": f("B")}, base={})
        self.assertEqual(self.paths(p, "download"), ["b.txt"])

    def test_deleted_here_is_deleted_there(self):
        p = self.plan(local={}, remote={"b.txt": f("B")}, base={"b.txt": f("B")})
        self.assertEqual(self.paths(p, "deleteRemote"), ["b.txt"])
        self.assertEqual(p["download"], [])

    def test_untouched_files_do_nothing(self):
        p = self.plan(local={"a.txt": f("A")}, remote={"a.txt": f("A")}, base={"a.txt": f("A")})
        self.assertEqual(p["unchanged"], 1)
        for k in ("upload", "download", "deleteLocal", "deleteRemote", "conflicts"):
            self.assertEqual(p[k], [], f"{k} should be empty for an untouched file")

    # ---- conflicts ------------------------------------------------------------------------

    def test_edited_on_both_keeps_both(self):
        p = self.plan(local={"doc.txt": f("LOCAL")},
                      remote={"doc.txt": dict(f("REMOTE"), device="phone")},
                      base={"doc.txt": f("OLD")})
        self.assertEqual(p["upload"], [])
        self.assertEqual(p["download"], [])
        self.assertEqual(len(p["conflicts"]), 1)
        c = p["conflicts"][0]
        self.assertEqual(c["path"], "doc.txt")
        self.assertIn("conflict from phone", c["keepAs"])
        self.assertTrue(c["keepAs"].endswith(".txt"),
                        f"the suffix must go BEFORE the extension, got {c['keepAs']}")

    def test_identical_edits_are_not_a_conflict(self):
        """A photo library seeded from the same camera on two devices must not explode into
        thousands of conflict copies the first time it syncs."""
        p = self.plan(local={"IMG_1.jpg": f("SAME")}, remote={"IMG_1.jpg": f("SAME")}, base={})
        self.assertEqual(p["conflicts"], [])
        self.assertEqual(p["upload"], [])
        self.assertEqual(p["download"], [])

    # ---- delete vs edit -------------------------------------------------------------------

    def test_deleted_here_but_edited_there_keeps_the_edit(self):
        p = self.plan(local={}, remote={"x.txt": f("NEW")}, base={"x.txt": f("OLD")})
        self.assertEqual(self.paths(p, "download"), ["x.txt"])
        self.assertEqual(p["deleteRemote"], [])

    def test_deleted_there_but_edited_here_keeps_the_edit(self):
        p = self.plan(local={"x.txt": f("NEW")}, remote={}, base={"x.txt": f("OLD")})
        self.assertEqual(self.paths(p, "upload"), ["x.txt"])
        self.assertEqual(p["deleteLocal"], [])

    def test_deleted_on_both_is_not_an_action(self):
        p = self.plan(local={}, remote={}, base={"x.txt": f("OLD")})
        for k in ("upload", "download", "deleteLocal", "deleteRemote", "conflicts"):
            self.assertEqual(p[k], [], f"{k} should be empty when both sides deleted it")

    def test_a_tombstone_reads_as_deleted(self):
        """The manifest keeps deletions as tombstones so other devices learn about them; a tombstone
        must behave exactly like absence, or every delete becomes a conflict."""
        p = self.plan(local={"x.txt": f("A")}, remote={"x.txt": {"deletedAt": 2 * DAY}},
                      base={"x.txt": f("A")})
        self.assertEqual(self.paths(p, "deleteLocal"), ["x.txt"])

    # ---- filesystem reality ---------------------------------------------------------------

    def test_mtime_slop_does_not_report_every_file_as_changed(self):
        """exFAT rounds to 2s; SMB and Android's SAF round too. Without tolerance a removable drive
        re-uploads itself on every sweep."""
        p = self.plan(local={"a.txt": {"size": 10, "mtime": 1000}},
                      remote={"a.txt": {"size": 10, "mtime": 2000}},
                      base={"a.txt": {"size": 10, "mtime": 1000}})
        self.assertEqual(p["unchanged"], 1, "a 1s mtime difference is not a change")

    def test_sha_beats_mtime_when_both_sides_have_one(self):
        """A file restored from a backup has new mtimes and identical bytes. It is not a change."""
        p = self.plan(local={"a.txt": {"csum": "A", "size": 10, "mtime": 999999}},
                      remote={"a.txt": {"csum": "A", "size": 10, "mtime": 1000}},
                      base={"a.txt": {"csum": "A", "size": 10, "mtime": 1000}})
        self.assertEqual(p["unchanged"], 1)

    def test_a_changed_file_is_still_caught_when_the_size_matches(self):
        """Same length, different bytes — an edit that a size-only check would miss."""
        p = self.plan(local={"a.txt": {"csum": "NEW", "size": 10, "mtime": 1000}},
                      remote={"a.txt": {"csum": "OLD", "size": 10, "mtime": 1000}},
                      base={"a.txt": {"csum": "OLD", "size": 10, "mtime": 1000}})
        self.assertEqual(self.paths(p, "upload"), ["a.txt"])

    # ---- trash + bookkeeping ---------------------------------------------------------------

    def test_local_deletions_go_to_a_dated_trash(self):
        js = ("const S=require(%s);"
              "process.stdout.write(JSON.stringify([S.trashPath('sub/a.txt', %d),"
              " S.conflictPath('sub/a.txt','phone',%d), S.conflictPath('README','phone',%d)]));"
              ) % (json.dumps(MOD), 5 * DAY, 5 * DAY, 5 * DAY)
        out = json.loads(subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                                        timeout=60).stdout)
        self.assertTrue(out[0].startswith(".pc-trash/1970-01-06/"), out[0])
        self.assertTrue(out[0].endswith("sub/a.txt"), out[0])
        self.assertEqual(out[1], "sub/a (conflict from phone, 1970-01-06).txt")
        self.assertEqual(out[2], "README (conflict from phone, 1970-01-06)",
                         "a file with no extension must not grow a stray dot")

    def test_advance_makes_the_next_run_a_no_op(self):
        """Folding a completed plan back into `base` is what stops a sync looping — re-uploading
        what it just downloaded, forever."""
        js = ("const S=require(%s);"
              "const base=S.advance({base:{}, done:{'a.txt':{sha:'A',size:10,mtime:1000}}, now:%d});"
              "const p=S.diff({local:{'a.txt':{sha:'A',size:10,mtime:1000}},"
              " remote:{'a.txt':{sha:'A',size:10,mtime:1000}}, base});"
              "process.stdout.write(JSON.stringify({base,p}));") % (json.dumps(MOD), 5 * DAY)
        out = json.loads(subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                                        timeout=60).stdout)
        self.assertEqual(out["p"]["unchanged"], 1)
        self.assertEqual(out["p"]["upload"], [])
        self.assertEqual(out["p"]["download"], [])

    def test_tombstones_are_only_dropped_when_old(self):
        js = ("const S=require(%s);"
              "const m={'keep.txt':{deletedAt:%d},'old.txt':{deletedAt:%d},'live.txt':{sha:'A'}};"
              "process.stdout.write(JSON.stringify(S.pruneTombstones(m, %d, %d)));"
              ) % (json.dumps(MOD), 29 * DAY, 1 * DAY, 30 * DAY, 40 * DAY)
        out = json.loads(subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                                        timeout=60).stdout)
        self.assertIn("keep.txt", out, "a tombstone younger than the window must survive — an "
                                       "offline device has not seen it yet")
        self.assertNotIn("old.txt", out)
        self.assertIn("live.txt", out)


class TestExcludesAreCaseInsensitive(unittest.TestCase):
    """Typing a folder name means the folder you can see.

    Windows and Android both display `Old` and accept `old`, so a pattern typed on one device
    excluded a folder and the same pattern typed on another did not — and the devices then synced
    different numbers of files. Reported as exactly that: "all three exclude a folder called old, yet
    the number of files synced is inconsistent".

    Broadening a pattern is the safe direction. An exclusion drops a path from ALL THREE snapshots,
    so it can never delete anything anywhere; the worst case is a folder that stops syncing until the
    pattern is made more specific.
    """

    def excluded(self, path, patterns):
        js = "const S = require(%s);\n" % json.dumps(MOD) + textwrap.dedent("""
          const f = S.excluder(%s);
          process.stdout.write(JSON.stringify(f(%s)));
        """ % (json.dumps(patterns), json.dumps(path)))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_lowercase_pattern_matches_the_folder_as_shown(self):
        self.assertTrue(self.excluded("Old/2019/img.jpg", ["old"]))
        self.assertTrue(self.excluded("Pictures/OLD/x.png", ["old"]))

    def test_uppercase_pattern_matches_a_lowercase_folder(self):
        self.assertTrue(self.excluded("old/2019/img.jpg", ["Old"]))

    def test_it_still_only_matches_that_name(self):
        """Case-insensitive is not vague — a different folder is still a different folder."""
        self.assertFalse(self.excluded("older/2019/img.jpg", ["old"]))
        self.assertFalse(self.excluded("Pictures/holiday.jpg", ["old"]))

    def test_globs_keep_working(self):
        self.assertTrue(self.excluded("a/b/CACHE/x", ["**/cache"]))
        self.assertTrue(self.excluded("Thumbs.DB", ["*.db"]))


class TestRedundantConflicts(unittest.TestCase):
    """Which conflict copies can be PROVEN redundant.

    Rounds of getting content identity wrong produced copies of files that were never different, and
    they are ordinary files now. This picks the ones that are demonstrably the same file — and the
    proof has to be real, because the alternative is deleting someone's work.
    """

    def run_js(self, body):
        js = "const S = require(%s);\n%s" % (json.dumps(MOD), textwrap.dedent(body))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-2000:])
        return json.loads(r.stdout)

    def found(self, manifest):
        return self.run_js("""
          const man = %s;
          process.stdout.write(JSON.stringify(S.redundantConflicts(man).map(x => x.path)));
        """ % json.dumps(manifest))

    def test_identical_by_checksum_is_redundant(self):
        got = self.found({
            "photo.jpg": {"csum": "A", "size": 10, "mtime": 1},
            "photo (conflict from tablet, 2026-08-10).jpg": {"csum": "A", "size": 10, "mtime": 2},
        })
        self.assertEqual(got, ["photo (conflict from tablet, 2026-08-10).jpg"])

    def test_identical_by_chunk_list_is_redundant(self):
        got = self.found({
            "clip.mp4": {"chunks": ["x", "y"], "size": 99, "mtime": 1},
            "clip (conflict from laptop, 2026-08-09).mp4": {"chunks": ["x", "y"], "size": 99, "mtime": 9},
        })
        self.assertEqual(len(got), 1)

    def test_a_copy_that_differs_is_left_alone(self):
        """The whole point of a conflict copy. Two people edited the same file; both survive."""
        got = self.found({
            "notes.txt": {"csum": "B", "size": 5, "mtime": 1},
            "notes (conflict from phone, 2026-08-10).txt": {"csum": "C", "size": 5, "mtime": 1},
        })
        self.assertEqual(got, [])

    def test_size_and_mtime_alone_are_not_proof(self):
        """STRICTER THAN same() on purpose. A copy taken FROM a file has the same size and timestamp
        whether or not the bytes match, so that is not evidence of anything — and this deletes."""
        got = self.found({
            "sized.bin": {"size": 7, "mtime": 1},
            "sized (conflict from x, 2026-08-10).bin": {"size": 7, "mtime": 1},
        })
        self.assertEqual(got, [])

    def test_a_copy_whose_original_is_gone_is_left_alone(self):
        """Then it is the only remaining copy of that content, whatever its name says."""
        got = self.found({"lonely (conflict from x, 2026-08-10).txt": {"csum": "Z", "size": 1, "mtime": 1}})
        self.assertEqual(got, [])

    def test_a_tombstoned_original_does_not_make_the_copy_redundant(self):
        got = self.found({
            "gone.txt": {"deletedAt": 123},
            "gone (conflict from x, 2026-08-10).txt": {"csum": "Q", "size": 1, "mtime": 1},
        })
        self.assertEqual(got, [])

    def test_an_ordinary_file_is_never_touched(self):
        got = self.found({"my (conflict) notes.txt": {"csum": "A", "size": 1, "mtime": 1},
                          "holiday photos/beach.jpg": {"csum": "A", "size": 1, "mtime": 1}})
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(not NODE, "no node on this node")
class TestSyncPolicy(unittest.TestCase):
    """WHEN to sync — which on a phone matters more than how.

    The expensive things are the radio (an upload holds it awake far longer than the bytes suggest),
    hashing a large tree, and waking up at all. Each case here is one of those bills.
    """

    def ask(self, state, prefs=None):
        js = ("const S=require(%s);"
              "process.stdout.write(JSON.stringify(S.shouldSync(%s, %s)));"
              ) % (json.dumps(MOD), json.dumps(state), json.dumps(prefs or {}))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_only_when_plugged_in_waits(self):
        r = self.ask({"charging": False, "online": True}, {"onlyWhenCharging": True})
        self.assertEqual(r["mode"], "none")
        self.assertIn("plug in", r["why"])

    def test_only_when_plugged_in_runs_on_the_charger(self):
        r = self.ask({"charging": True, "online": True, "now": 10 ** 9},
                     {"onlyWhenCharging": True})
        self.assertTrue(r["run"])

    def test_metered_data_is_refused_by_default(self):
        """wifiOnly defaults on: mobile data is both a bill and the radio's worst case."""
        r = self.ask({"charging": True, "online": True, "metered": True})
        self.assertEqual(r["mode"], "none")
        self.assertIn("Wi-Fi", r["why"])

    def test_a_low_battery_still_notices_changes(self):
        """Degrade, don't stop. Learning what changed is one small request; it is the UPLOAD and the
        rehash that cost, and those can wait for the charger."""
        r = self.ask({"charging": False, "online": True, "battery": 9})
        self.assertEqual(r["mode"], "metadata")

    def test_a_low_battery_on_the_charger_is_not_low(self):
        r = self.ask({"charging": True, "online": True, "battery": 9, "now": 10 ** 9})
        self.assertNotEqual(r["mode"], "metadata")

    def test_a_full_rehash_is_a_charging_time_job(self):
        r = self.ask({"charging": True, "online": True, "now": 10 ** 9, "lastFullScanAt": 0,
                      "lastSyncAt": 1})
        self.assertEqual(r["mode"], "full")

    def test_on_battery_it_never_rehashes_the_whole_tree(self):
        r = self.ask({"charging": False, "online": True, "battery": 90, "now": 10 ** 9,
                      "lastFullScanAt": 0, "lastSyncAt": 1})
        self.assertEqual(r["mode"], "incremental")

    def test_it_does_not_wake_for_nothing(self):
        now = 10 ** 9
        r = self.ask({"charging": True, "online": True, "now": now, "lastSyncAt": now - 60_000})
        self.assertEqual(r["mode"], "none")

    def test_a_known_change_beats_the_interval(self):
        now = 10 ** 9
        r = self.ask({"charging": True, "online": True, "now": now, "lastSyncAt": now - 60_000,
                      "dirty": True})
        self.assertTrue(r["run"])

    def test_pressing_the_button_always_works(self):
        """Refusing someone who just pressed Sync because the battery is at 19% is how a feature
        earns a reputation. It runs whatever the battery, the network and the interval say."""
        r = self.ask({"manual": True, "charging": False, "battery": 5, "metered": True},
                     {"onlyWhenCharging": True, "wifiOnly": True})
        self.assertTrue(r["run"])

    def test_pressing_the_button_does_not_rehash_the_whole_folder(self):
        """It asks for a SYNC, not for a rehash of everything. `full` re-reads and re-hashes every
        file, which on a 15790-file folder is minutes of disk naming each file as it goes — from the
        outside, indistinguishable from the sync starting over, and reported as exactly that."""
        r = self.ask({"manual": True, "charging": False, "battery": 5, "metered": True},
                     {"onlyWhenCharging": True, "wifiOnly": True})
        self.assertEqual(r["mode"], "incremental")

    def test_a_deep_check_still_rehashes(self):
        """For the case size+mtime cannot see: a file edited in place, same size, inside the slop."""
        r = self.ask({"manual": True, "deep": True, "charging": False}, {})
        self.assertEqual(r["mode"], "full")

    def test_a_new_folder_does_not_start_on_its_own(self):
        """Adding a folder used to arm it immediately — the watcher fires, a resume or a focus nudges,
        and the first sweep is under way before anyone has typed a line into "Don't sync these".
        Reported from a phone: "the apk is syncing before I can put what to exclude". That first sweep
        is also the expensive one, and the one that publishes the folder to every other device."""
        r = self.ask({"charging": True, "dirty": True}, {"paused": True})
        self.assertEqual(r["mode"], "none")
        self.assertIn("Start", r["why"])

    def test_start_overrides_paused(self):
        """Start (and Check) are how someone LEAVES that state, so the button still runs."""
        r = self.ask({"manual": True, "charging": False}, {"paused": True})
        self.assertTrue(r["run"])

    def test_offline_does_nothing(self):
        self.assertEqual(self.ask({"online": False, "charging": True})["mode"], "none")

    def test_disabled_does_nothing(self):
        self.assertEqual(self.ask({"charging": True}, {"enabled": False})["mode"], "none")


@unittest.skipIf(not NODE, "no node on this node")
class TestExclusions(unittest.TestCase):
    """"All of Pictures except Old."

    The matching is the easy half. The half that matters is WHERE it is applied: an exclusion that
    only filtered the local scan would make every already-synced file under Pictures/Old look
    "deleted here", and this engine would faithfully delete them from every other device. Excluding
    a folder means "stop looking at it", never "delete it".
    """

    def plan(self, local, remote, base, excludes):
        js = ("const S=require(%s);"
              "process.stdout.write(JSON.stringify(S.diff({local:%s, remote:%s, base:%s,"
              " excludes:%s, device:'laptop', now:%d})));"
              ) % (json.dumps(MOD), json.dumps(local), json.dumps(remote), json.dumps(base),
                   json.dumps(excludes), 5 * DAY)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-2000:])
        return json.loads(r.stdout)

    def match(self, patterns, paths):
        js = ("const S=require(%s); const ex=S.excluder(%s);"
              "process.stdout.write(JSON.stringify(%s.map(p=>ex(p))));"
              ) % (json.dumps(MOD), json.dumps(patterns), json.dumps(paths))
        return json.loads(subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                                         timeout=60).stdout)

    def test_a_folder_name_excludes_everything_under_it(self):
        got = self.match(["Old"], ["Old", "Old/2019/img.jpg", "Trips/Old/b.jpg", "Older/a.jpg",
                                   "keep/a.jpg"])
        self.assertEqual(got, [True, True, True, False, False],
                         "a folder name must catch its contents, and must not match a PREFIX of "
                         "another name (Older is not Old)")

    def test_anchoring_and_wildcards(self):
        self.assertEqual(self.match(["/Trips/Raw"], ["Trips/Raw/x", "x/Trips/Raw/y"]), [True, False],
                         "a leading slash anchors to the top of the sync folder")
        self.assertEqual(self.match(["*.tmp"], ["a.tmp", "sub/b.tmp", "a.tmpx"]), [True, True, False])
        self.assertEqual(self.match(["**/cache"], ["deep/x/cache/f", "cache/f"]), [True, True])

    def test_excluding_a_folder_does_not_delete_it_elsewhere(self):
        """THE one that matters. Pictures/Old is already synced; this device now excludes it. The
        remote copy must be left completely alone."""
        p = self.plan(local={"a.jpg": f("A")},
                      remote={"a.jpg": f("A"), "Old/x.jpg": f("X")},
                      base={"a.jpg": f("A"), "Old/x.jpg": f("X")},
                      excludes=["Old"])
        self.assertEqual(p["deleteRemote"], [],
                         "adding an exclusion deleted the other devices' copies")
        self.assertEqual(p["deleteLocal"], [])
        self.assertEqual(p["download"], [])
        self.assertEqual(p["excluded"], 1)

    def test_an_excluded_path_is_never_downloaded(self):
        """Another device syncs Pictures/Old; this one excluded it. It must not arrive here."""
        p = self.plan(local={}, remote={"Old/x.jpg": f("X")}, base={}, excludes=["Old"])
        self.assertEqual(p["download"], [])
        self.assertEqual(p["excluded"], 1)

    def test_an_excluded_local_file_is_never_uploaded(self):
        p = self.plan(local={"Old/x.jpg": f("X"), "a.jpg": f("A")}, remote={}, base={},
                      excludes=["Old"])
        self.assertEqual(sorted(a["path"] for a in p["upload"]), ["a.jpg"])

    def test_no_patterns_excludes_nothing(self):
        p = self.plan(local={"a.jpg": f("A")}, remote={}, base={}, excludes=[])
        self.assertEqual(len(p["upload"]), 1)
        self.assertEqual(p["excluded"], 0)


@unittest.skipIf(not NODE, "no node on this node")
class TestMassDelete(unittest.TestCase):
    """THE SHAPE OF THE ANSWER, which nothing above this ever looked at.

    Every case in TestFolderSync asserts the decision for ONE path, and every one of those decisions
    is correct. That is not enough, and a real Pictures folder is what proved it: the shared manifest
    held ~10k paths and every single one was a tombstone (`n=0` live on the server). A device that
    still had the files re-added the folder, read "deleted elsewhere" for all 10k — correctly, per
    path, by the rules above — and moved the entire folder into `.pc-trash` without asking.

    The server's collapse guard could not catch it, because a mass LOCAL delete writes no manifest at
    all: it only advances `base`. So the guard has to live here, and it is the phone book's rule —
    refuse to delete more than you keep.
    """

    def mass(self, plan):
        """The plan is BUILT IN NODE from counts, never serialised into argv — the case this guard
        exists for is ten thousand paths, and `node -e` with ten thousand paths in it is E2BIG."""
        js = (
            "const S=require(%s);"
            "const s=%s;"
            "const list=(n,w)=>Array.from({length:n},(_,i)=>({path:'p'+i+'.jpg',why:w}));"
            "const plan={upload:list(s.upload,'new here'),download:list(s.download,'new elsewhere'),"
            " conflicts:list(s.conflicts,'both'),notes:list(s.notes,s.noteWhy),"
            " deleteLocal:list(s.deleteLocal,'deleted elsewhere'),deleteRemote:[],unchanged:s.unchanged};"
            "process.stdout.write(JSON.stringify(S.massDelete(plan)));"
        ) % (json.dumps(MOD), json.dumps(plan))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-2000:])
        return json.loads(r.stdout)

    def plan_of(self, n_delete, keep=0, notes=0, note_why="same content both sides",
                download=0, upload=0, conflicts=0):
        return {"deleteLocal": n_delete, "unchanged": keep, "notes": notes, "noteWhy": note_why,
                "download": download, "upload": upload, "conflicts": conflicts}

    def test_the_production_shape_is_refused(self):
        """10k tombstones, nothing kept. The exact manifest that emptied a Pictures folder."""
        m = self.mass(self.plan_of(10142))
        self.assertIsNotNone(m, "a sweep that trashes the whole folder and keeps nothing was allowed")
        self.assertEqual(m["n"], 10142)
        self.assertEqual(m["keep"], 0)

    def test_deleting_a_few_files_is_never_questioned(self):
        """The normal working of the feature. A confirmation on every 3-file delete is how people
        learn to click through the one that matters."""
        self.assertIsNone(self.mass(self.plan_of(3)))
        self.assertIsNone(self.mass(self.plan_of(19)),
                          "the floor is what keeps ordinary deletes silent")

    def test_a_delete_smaller_than_what_survives_is_allowed(self):
        """Tidying 50 files out of a 5000-file folder is a delete, not a wipe."""
        self.assertIsNone(self.mass(self.plan_of(50, keep=4950)))

    def test_kept_counts_every_way_a_file_survives(self):
        """`unchanged` is not the only thing left standing when the sweep ends — a file being
        downloaded, uploaded or kept as a conflict copy is still a file in the folder. Counting only
        one of them would refuse an ordinary big first sync."""
        self.assertIsNone(self.mass(self.plan_of(30, download=40)))
        self.assertIsNone(self.mass(self.plan_of(30, notes=40)),
                          "files both sides already agree on are kept files")

    def test_deleted_on_both_is_not_a_kept_file(self):
        """It has no bytes anywhere, so counting it would only make the guard quieter — and quieter
        is the direction that lost the pictures."""
        self.assertIsNotNone(self.mass(self.plan_of(30, notes=40, note_why="deleted on both")))

    def test_exactly_as_many_deleted_as_kept_is_allowed(self):
        """The rule is 'more than you keep', and a boundary that drifts turns a silent guard into a
        noisy one or the other way about."""
        self.assertIsNone(self.mass(self.plan_of(25, keep=25)))
        self.assertIsNotNone(self.mass(self.plan_of(26, keep=25)))
