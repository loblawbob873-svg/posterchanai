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
ENGINE = os.path.join(os.path.dirname(MOD), "syncstate.js")
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
        """The rules, against the CURRENT engine, stated the way this file has always stated them.

        The decision code has moved twice — foldersync.js → syncengine.js (one document per device)
        → syncstate.js (one versioned record per file) — but the RULES did not, and these are the
        rules. So the inputs are translated rather than the tests rewritten: `remote` becomes the
        per-file record set, `base` this device's journal, and the translator synthesises the
        VERSIONS the fixtures predate — a record whose content matches the journal shares its
        version, one that differs is one ahead, which is exactly what the server's counter would
        have produced.
        """
        def _addr(e):
            e = dict(e)
            if not e.get("deletedAt") and "sha" not in e:
                e["sha"] = "b_" + str(e.get("csum", e.get("mtime", "x")))
            return e
        def _same(a, b):
            return (a.get("csum") == b.get("csum")
                    and bool(a.get("deletedAt")) == bool(b.get("deletedAt")))
        state, index = {}, {}
        for path, e in (base or {}).items():
            local_stat = {k: e[k] for k in ("size", "mtime", "csum") if k in e}
            index[path] = dict(_addr(e), v=1, local=local_stat)
        for path, e in (remote or {}).items():
            b = (base or {}).get(path)
            v = 1 if (b is not None and _same(e, b)) else (2 if b is not None else 1)
            state[path] = dict(_addr(e), v=v, by=e.get("device", "phone"))
        js = (
            "const path=require('path');"
            "require(%s);"
            "const E=require(%s);"
            "const p=E.plan({disk:%s, state:%s, index:%s, device:%s, now:%d});"
            "process.stdout.write(JSON.stringify({upload:p.send, download:p.fetch,"
            " deleteLocal:p.trash, deleteRemote:p.tombstone, conflicts:p.keepBoth, notes:p.settle,"
            " unchanged:p.unchanged, excluded:p.excluded}));"
        ) % (json.dumps(MOD), json.dumps(ENGINE), json.dumps(local), json.dumps(state),
             json.dumps(index), json.dumps(device), now)
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
        """Same local shape as the case above — only the TOMBSTONE tells them apart.

        A deletion is a tombstone somebody published, never a path missing from a document. That
        distinction is the whole per-device design: a view that failed to load, or came back empty,
        used to be indistinguishable from "every file you have was deleted", and it emptied a real
        Pictures folder into the trash.
        """
        p = self.plan(local={"a.txt": f("A")}, remote={"a.txt": {"deletedAt": 2 * DAY}},
                      base={"a.txt": f("A")})
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
        p = self.plan(local={}, remote={"x.txt": {"deletedAt": 2 * DAY}},
                      base={"x.txt": {"deletedAt": 2 * DAY}})
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
        entry = {"csum": "A", "sha": "b_A", "size": 10, "mtime": 1000}
        js = ("const S=require(%s); const E=require(%s);"
              "const base=S.advance({base:{}, done:{'a.txt':%s}, now:%d});"
              "const idx={'a.txt': Object.assign({}, base['a.txt'], {v:1, local:%s})};"
              "const st={'a.txt': Object.assign({}, %s, {v:1})};"
              "const p=E.plan({disk:{'a.txt':%s}, state:st,"
              " index:idx, device:'laptop', now:%d});"
              "process.stdout.write(JSON.stringify({base, unchanged:p.unchanged,"
              " upload:p.send, download:p.fetch}));"
              ) % (json.dumps(MOD), json.dumps(ENGINE), json.dumps(entry), 5 * DAY,
                   json.dumps(entry), json.dumps(entry), json.dumps(entry), 5 * DAY)
        out = json.loads(subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                                        timeout=60).stdout)
        self.assertEqual(out["unchanged"], 1)
        self.assertEqual(out["upload"], [])
        self.assertEqual(out["download"], [])

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
        index = {}
        for path, e in (base or {}).items():
            index[path] = dict(e, local={k: e[k] for k in ("size", "mtime", "csum") if k in e})
        js = ("require(%s); const E=require(%s);"
              "const st=%s;"
              "const p=E.plan({disk:%s, state:st, index:%s,"
              " excludes:%s, device:'laptop', now:%d});"
              "process.stdout.write(JSON.stringify({upload:p.send, download:p.fetch,"
              " deleteLocal:p.trash, deleteRemote:p.tombstone, conflicts:p.keepBoth,"
              " notes:p.settle, unchanged:p.unchanged, excluded:p.excluded}));"
              ) % (json.dumps(MOD), json.dumps(ENGINE), json.dumps(remote), json.dumps(local),
                   json.dumps(index), json.dumps(excludes), 5 * DAY)
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
        """The SHORT-LIST verdict — "this sweep removes more than it keeps" — and only that one.

        Two rules raise `massTrash` now: this proportional one, and an absolute floor at FLOOR that
        stops any bulk deletion an unattended sweep would otherwise apply (see
        test_delete_and_restore_symmetry.py — 59 stale tombstones over a 1,000-file folder passed
        every proportional test there was and emptied the same files off three devices in turn).
        Filtering on `rule` keeps this class measuring what it was written to measure: the `keep`
        accounting, which the floor does not exercise and which still has to be exactly right.
        """
        js = ("require(%s); const E=require(%s);"
              "const c=%s;"
              "const mk=(n,extra)=>Array.from({length:n},(_,i)=>Object.assign({path:'p'+i},extra||{}));"
              "const plan={fetch:mk(c.download), send:mk(c.upload), trash:mk(c.deleteLocal),"
              " tombstone:[], keepBoth:mk(c.conflicts), settle:mk(c.notes,{why:c.noteWhy}),"
              " unchanged:c.unchanged, excluded:0};"
              "const v=E.check(plan, {}).find(x=>x.kind==='massTrash'&&x.rule==='shortList')||null;"
              "process.stdout.write(JSON.stringify(v));"
              ) % (json.dumps(MOD), json.dumps(ENGINE), json.dumps(plan))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-2000:])
        return json.loads(r.stdout)

    def any_rule(self, plan):
        """Whether the sweep would be stopped AT ALL — either rule, which is what a person on the
        other end actually experiences."""
        js = ("require(%s); const E=require(%s);"
              "const c=%s;"
              "const mk=(n,extra)=>Array.from({length:n},(_,i)=>Object.assign({path:'p'+i},extra||{}));"
              "const plan={fetch:mk(c.download), send:mk(c.upload), trash:mk(c.deleteLocal),"
              " tombstone:[], keepBoth:mk(c.conflicts), settle:mk(c.notes,{why:c.noteWhy}),"
              " unchanged:c.unchanged, excluded:0};"
              "process.stdout.write(JSON.stringify("
              "  E.check(plan, {}).some(x=>x.kind==='massTrash')));"
              ) % (json.dumps(MOD), json.dumps(ENGINE), json.dumps(plan))
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

    def test_a_delete_smaller_than_what_survives_passes_the_ratio(self):
        """Tidying 50 files out of a 5000-file folder is not a WIPE, and the proportional rule is
        right not to call it one. It is still a bulk delete, so the absolute floor asks anyway —
        that is the point of having both, and it is asserted just below."""
        self.assertIsNone(self.mass(self.plan_of(50, keep=4950)))
        self.assertTrue(self.any_rule(self.plan_of(50, keep=4950)),
                        "50 unattended deletions on a big folder ran with nothing asked")

    def test_kept_counts_every_way_a_file_survives(self):
        """`unchanged` is not the only thing left standing when the sweep ends — a file being
        downloaded, uploaded or kept as a conflict copy is still a file in the folder. Counting only
        one of them would refuse an ordinary big first sync."""
        self.assertIsNone(self.mass(self.plan_of(30, download=40)))
        self.assertIsNone(self.mass(self.plan_of(30, notes=40)),
                          "files both sides already agree on are kept files")
        # …and the floor still speaks for all of them, because 30 is a bulk delete either way.
        self.assertTrue(self.any_rule(self.plan_of(30, download=40)))

    def test_deleted_on_both_is_not_a_kept_file(self):
        """It has no bytes anywhere, so counting it would only make the guard quieter — and quieter
        is the direction that lost the pictures."""
        self.assertIsNotNone(self.mass(self.plan_of(30, notes=40, note_why="deleted on both")))

    def test_the_ratio_boundary_is_exactly_more_than_you_keep(self):
        """A boundary that drifts turns a silent guard into a noisy one or the other way about. This
        is the ratio rule's own boundary; the floor's is asserted in
        test_delete_and_restore_symmetry.py, and below FLOOR neither speaks."""
        self.assertIsNone(self.mass(self.plan_of(25, keep=25)))
        self.assertIsNotNone(self.mass(self.plan_of(26, keep=25)))
        self.assertFalse(self.any_rule(self.plan_of(19, keep=19)),
                         "19 deletions must stay silent — the floor is 20")


@unittest.skipIf(not NODE, "no node on this node")
class TestConflictCandidates(unittest.TestCase):
    """Which conflict copies are worth OPENING, when the manifest cannot judge them.

    `redundantConflicts` is the proof — a matching checksum or chunk list, nothing else — and it is
    right to be strict, because for a copy taken FROM a file, equal size and mtime is exactly what
    you would expect whether the bytes match or not. But a manifest only carries an identity for
    entries some sweep uploaded with one, and a folder that has been through several devices has
    plenty that do not. Reported from three machines at once: a phone downloading a pile of
    `(conflict from windows, …)` copies while every device answered "no conflict copies that are
    provably identical".

    So this is the shortlist a device holding both files can settle exactly by hashing them. It
    decides what to read; the hashes still decide what to delete.
    """

    def cand(self, manifest):
        js = ("const S=require(%s);"
              "process.stdout.write(JSON.stringify(S.conflictCandidates(%s)));"
              ) % (json.dumps(MOD), json.dumps(manifest))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError("node failed:\n" + r.stderr[-2000:])
        return json.loads(r.stdout)

    C = "note (conflict from windows, 2026-08-15).txt"

    def test_a_pair_the_manifest_cannot_judge_is_offered_for_reading(self):
        out = self.cand({"note.txt": {"sha": "A", "size": 10, "mtime": 1},
                         self.C: {"sha": "B", "size": 10, "mtime": 2}})
        self.assertEqual([c["path"] for c in out], [self.C])
        self.assertEqual(out[0]["original"], "note.txt")

    def test_a_pair_the_manifest_already_proved_is_not_offered_again(self):
        """`redundantConflicts` has it; reading the files would be wasted I/O."""
        out = self.cand({"note.txt": {"csum": "X", "size": 10},
                         self.C: {"csum": "X", "size": 10}})
        self.assertEqual(out, [])

    def test_different_sizes_are_not_even_candidates(self):
        """Cheap pre-filter — it keeps the caller from opening every unrelated pair in the folder.
        It is NOT the verdict: equal size proves nothing, which is the whole reason
        redundantConflicts refuses to use it."""
        out = self.cand({"note.txt": {"sha": "A", "size": 10},
                         self.C: {"sha": "B", "size": 11}})
        self.assertEqual(out, [])

    def test_a_copy_whose_original_was_deleted_is_left_alone(self):
        """It is not a redundant copy, it is the only remaining copy — and tonight's folder is full
        of originals that were tombstoned."""
        # The tombstone carries a SIZE on purpose: without it the size pre-filter excludes this
        # pair anyway and the test would pass whether or not the tombstone guard exists.
        out = self.cand({"note.txt": {"deletedAt": 9000, "size": 10},
                         self.C: {"sha": "B", "size": 10}})
        self.assertEqual(out, [])

    def test_a_tombstoned_copy_is_left_alone(self):
        out = self.cand({"note.txt": {"sha": "A", "size": 10},
                         self.C: {"deletedAt": 9000, "size": 10}})   # size, so only live() can catch it
        self.assertEqual(out, [])

    def test_an_ordinary_file_is_never_a_candidate(self):
        out = self.cand({"note.txt": {"sha": "A", "size": 10},
                         "holiday.jpg": {"sha": "B", "size": 10}})
        self.assertEqual(out, [])

    def test_the_suffix_is_read_before_the_extension(self):
        """conflictPath puts it there so the file still opens in whatever owns that type — a
        candidate matcher that assumed otherwise would pair every copy with a file that does not
        exist, and quietly find nothing for ever."""
        out = self.cand({"a/b/report.pdf": {"sha": "A", "size": 5},
                         "a/b/report (conflict from phone, 2026-08-09).pdf": {"sha": "B", "size": 5}})
        self.assertEqual([c["original"] for c in out], ["a/b/report.pdf"])


class AddIsAGrant(unittest.TestCase):
    """Adding a folder must not draw "Point at the folder again…" on a card that is already
    syncing. `granted` is fetched when the screen paints; the folder picked afterwards is not in
    that list, so the very first repaint called the brand-new folder lost — every add ("why do I
    always have to point at the folder again! it's syncing while that button is still there")."""

    def test_the_add_handler_records_the_pick_as_granted_before_painting(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        src = open(os.path.join(root, "static", "js", "client", "sync.js"), encoding="utf-8").read()
        at = src.index("const add = document.getElementById('sync-add')")
        seg = src[at:at + 6000]
        push = seg.index("granted.push({ id: picked.id")
        paint = seg.index("watch(picked.id); paint();")
        self.assertLess(push, paint,
                        "the pick is not recorded as a grant before the repaint — the fresh card "
                        "draws the re-link button while it syncs")


class LostIsNotAStringCompare(unittest.TestCase):
    """"prompted me to point at Pictures again despite syncing" — the lost verdict compared SAF URI
    STRINGS while the OS honours grants semantically, so an id stored by an older build (different
    percent-encoding) read as lost forever over a folder syncing on every sweep. Two rules, pinned:
    ids compare normalised, and a folder whose last sweep succeeded minutes ago is never lost."""

    def _seg(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        src = open(os.path.join(root, "static", "js", "client", "sync.js"), encoding="utf-8").read()
        at = src.index("const _gid = u =>")
        return src[at:at + 700]

    def test_ids_compare_normalised_and_a_recent_sweep_overrules(self):
        seg = self._seg()
        self.assertIn("decodeURIComponent", seg)
        self.assertIn("_gid(g.id) === _gid(f.id)", seg)
        self.assertIn("recentlyOk", seg)
        self.assertIn("f.lastSyncAt", seg)
        self.assertIn("lastScanOkAt", seg,
                      "the grant proof keys on a CLEAN sweep — a folder mid-recovery fails "
                      "transfers every sweep and the banner never clears")
        self.assertIn("< 900000", seg,
                      "lastSyncAt is MILLISECONDS — a seconds comparison makes recentlyOk always "
                      "true and a genuinely revoked grant never shows the banner")

    def test_the_normaliser_actually_equates_the_encodings(self):
        js = """
        const _gid = u => { try{ return decodeURIComponent(String(u||'')).replace(/\/+$/,''); }
                            catch(_){ return String(u||''); } };
        const a='content://com.android.externalstorage.documents/tree/primary%3APictures';
        const b='content://com.android.externalstorage.documents/tree/primary:Pictures/';
        process.stdout.write(JSON.stringify(_gid(a)===_gid(b)));
        """
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout, "true", r.stderr[-500:])


class AbsoluteTrashCap(unittest.TestCase):
    """"no way that i should have had deleted files, many!" — proportional guards wave hundreds of
    tombstones through on a big folder (6,000 survivors allow 5,999 trashes). No UNATTENDED sweep
    applies a BULK deletion; a deliberate mass delete passes allowMassTrash.

    The number was 100 and is now FLOOR (20), the same figure that has always governed putting files
    back. At 100, 59 stale tombstones over a 1,000-file folder still ran silently on three devices in
    turn while the one device holding the files was refused by the resurrect floor — the guard
    protecting the files was what guaranteed the deletions won. See
    tests/client/test_delete_and_restore_symmetry.py."""

    def _check(self, n_trash, allow=False):
        js = """
        const E = require(%s);
        const plan = { fetch:[], send:[], keepBoth:[], settle:[], tombstone:[],
                       trash: Array.from({length:%d}, (_,i)=>({path:'f'+i})),
                       unchanged: 10000, settledGone: 0, excluded: 0 };
        const out = E.check(plan, { state: {}%s });
        process.stdout.write(JSON.stringify(out.filter(v=>v.kind==='massTrash').length));
        """ % (json.dumps(os.path.join(REPO, "static", "js", "client", "syncstate.js")),
               n_trash, ", allowMassTrash: true" if allow else "")
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return json.loads(r.stdout)

    def test_hundreds_of_trashes_are_refused_even_with_thousands_surviving(self):
        self.assertGreater(self._check(500), 0,
                           "500 unattended deletions sailed past 10,000 survivors")

    def test_a_deliberate_mass_delete_still_works(self):
        self.assertEqual(self._check(500, allow=True), 0)

    def test_an_ordinary_sweep_is_untouched(self):
        """Below the floor, nothing is questioned — that is what keeps the dialog meaningful. The
        number moved from 100 to 20 when the two directions were made symmetric: 59 unattended
        deletions on a 1,000-file folder is what this guard was measured failing to catch."""
        self.assertEqual(self._check(19), 0)
        self.assertGreater(self._check(20), 0, "the floor is 20 and it must include 20")


class FilesTrashSurface(unittest.TestCase):
    """"Blossom should show the trash dirs and let you restore from that" — Files shows THIS
    device's trash for the synced folder being browsed, grouped by date, and every restore path
    funnels into PCSync.restoreTrash (one loop: never overwrite, per-op timeouts)."""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.app = open(os.path.join(root, "static", "js", "client", "app.js"), encoding="utf-8").read()
        self.sync = open(os.path.join(root, "static", "js", "client", "sync.js"), encoding="utf-8").read()

    def test_one_restore_loop_shared_by_card_and_files(self):
        self.assertIn("async function restoreTrash(folderId, only)", self.sync)
        self.assertIn("restoreTrash,", self.sync.split("window.PCSync = {")[1][:200],
                      "restoreTrash is not exported — Files would grow a second loop that drifts")
        a = self.sync.index("async function restoreTrash(")
        seg = self.sync[a:a + 2600]
        self.assertIn("confirmGone", seg)
        self.assertIn("timed(", seg, "the shared loop lost its per-operation timeouts")
        # the card must CALL the shared loop, not carry its own copy
        self.assertEqual(self.sync.count("Put ' + rows.length + ' file'"), 1,
                         "two confirm strings = two loops")

    def test_files_shows_the_trash_only_where_it_exists(self):
        """The trash is a FOLDER now, not a panel of dates — it used to render a collapsed block of
        dated groups with a Restore button each, which told you that "2026-08-19 · 107 files"
        existed and nothing about what they were. The gating rule is unchanged and is what this
        checks: a device with no filesystem bridge, or one browsing a folder it does not itself
        sync, has no local trash and must be offered no door to one."""
        a = self.app.index("const _inTrash = ")
        seg = self.app[a:a + 1800]
        self.assertIn("window.pcFs", seg, "the door would render on a phone with no bridge")
        self.assertIn("listTrash", seg)
        self.assertIn("_syncRoot", seg)
        self.assertIn("PCSync", seg)
        # ...and only when there is something in it. An empty folder with a permanent Trash row in
        # it is a control that never does anything.
        self.assertIn("_trashRows.length", seg, "the door renders even when the trash is empty")

    def test_the_trash_folder_carries_its_own_two_verbs(self):
        """Every ordinary row action is wrong inside the trash: Download offers bytes the manifest
        has no address for, Rename renames a dead copy, and "Delete on every device" would publish a
        deletion for a file that is ALREADY deleted — which is how a wave of stale tombstones
        starts. Two verbs belong here: put it back, or destroy this copy."""
        self.assertIn("tr-back", self.app)
        self.assertIn("tr-gone", self.app)
        self.assertIn("purgeTrash", self.app, "delete-one-copy has no bridge call behind it")
        a = self.app.index("const selbar = ")
        self.assertIn("!_inTrash", self.app[a:a + 200],
                      "the delete-on-every-device bar is shown inside the trash")


class AccountWideRestore(unittest.TestCase):
    """"a restore to all feature ... in Files-Blossom" — tombstones keep their address in BOTH
    executors, restoreMany republishes them live through the guarded edit path, and Files offers
    the block with per-file and restore-all actions."""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.app = open(os.path.join(root, "static", "js", "client", "app.js"), encoding="utf-8").read()
        self.sync = open(os.path.join(root, "static", "js", "client", "sync.js"), encoding="utf-8").read()
        self.exc = open(os.path.join(root, "static", "js", "client", "syncexec.js"), encoding="utf-8").read()
        self.java = open(os.path.join(root, "mobile", "android", "app", "src", "main", "java",
                                      "place", "poster", "app", "sync", "NativeSweep.java"),
                         encoding="utf-8").read()

    def test_tombstones_keep_the_address_in_both_executors(self):
        a = self.exc.index("for(const t of plan.tombstone)")
        # Wide enough to survive a comment: this reads SOURCE, so the window is about prose length
        # and not about the rule. The rule itself is exercised for real (a sweep is RUN, and the
        # published tombstone is inspected) in test_delete_and_restore_symmetry.py.
        seg = self.exc[a:a + 3000]
        for k in ("'sha'", "'chunks'", "'csum'"):
            self.assertIn(k, seg, "the JS tombstone forgets %s — account-wide restore dies" % k)
        j = self.java.index("plan.tombstone")
        jseg = self.java[j:j + 3000]        # same reason as above — a wider window, not a weaker rule
        self.assertIn('"sha"', jseg)
        self.assertIn('"chunks"', jseg)

    def test_restoreMany_republishes_only_addressed_tombstones(self):
        a = self.sync.index("async restoreMany(key, paths)")
        seg = self.sync[a:a + 1400]
        self.assertIn("deletedAt", seg)
        self.assertIn("unaddressed++", seg, "unrestorable entries are silently dropped")
        self.assertIn("api.put(p, live)", seg)

    def test_files_offers_the_block(self):
        self.assertIn("_fxDeletedHTML", self.app)
        self.assertIn("data-undelete", self.app)
        self.assertIn("fx-del-restoreall", self.app)
        a = self.app.index("function _fxDeletedHTML()")
        seg = self.app[a:a + 1800]
        self.assertIn(".pc-trash", seg, "trash-relative entries would be offered as restorable")


class ReclaimSeesEveryReference(unittest.TestCase):
    """"Device check never ends with space to reclaim!" — under per-device documents the reference
    collector kept missing classes of live blob (sealed path lists, then the kept prevSha
    generation), so the reclaim offer regenerated for ever. Per-file records have no side blobs to
    forget: the collector reads the record set itself, and the one thing it must not skip is
    TOMBSTONES — their retained addresses are what the account-wide Restore fetches."""

    def test_the_collector_reads_records_and_counts_tombstones(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        src = open(os.path.join(root, "static", "js", "client", "app.js"), encoding="utf-8").read()
        a = src.index("async function _syncRefIds")
        seg = src[a:a + 1600]
        self.assertIn("S.docs.state(", seg, "the reclaim no longer reads the record set")
        self.assertIn("TOMBSTONES COUNT TOO", seg,
                      "reclaiming a tombstone's bytes turns every Restore button into a 404")
        self.assertNotIn("deletedAt) continue", seg,
                         "tombstoned records are being skipped — their addresses are live references")


class OneSweepAtATime(unittest.TestCase):
    """"windows app ran out of memory" — Documents' upload lanes and Pictures' hashing first pass
    ran side by side in one renderer. The per-transfer backpressure bounds ONE sweep; nothing
    bounded two. A second folder queues, says so on its card, and starts by itself the moment the
    active sweep settles — it is never silently dropped."""

    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.sync = open(os.path.join(root, "static", "js", "client", "sync.js"),
                         encoding="utf-8").read()

    def test_a_second_folder_queues_instead_of_stacking(self):
        at = self.sync.index("ONE FOLDER'S REAL SWEEP AT A TIME")
        seg = self.sync[at:at + 1600]
        self.assertIn("running.size > 0", seg)
        self.assertIn("_syncQueue.set(f.id", seg)
        self.assertIn("waiting for", seg, "a queued folder must say so, or it reads as broken")

    def test_the_queue_is_drained_on_settle_never_dropped(self):
        # Drained from EVERY exit: the declines hand the slot back, and the sweep's own finally
        # drains after wakeEnd — so a THROWN sweep still starts the next folder.
        self.assertGreaterEqual(self.sync.count("_drainSyncQueue();"), 3)
        fin = self.sync.index("wakeEnd(); }catch(_){} }\n        _drainSyncQueue();")
        seg = self.sync[max(0, fin - 900):fin]
        self.assertIn("finally", seg)

    def test_previews_are_exempt(self):
        at = self.sync.index("ONE FOLDER'S REAL SWEEP AT A TIME")
        seg = self.sync[at:at + 1600]
        self.assertIn("!o.dryRun && running.size", seg,
                      "a Preview must not queue behind an hours-long seed")


if __name__ == "__main__":
    unittest.main()


class OneMappingPerPair(unittest.TestCase):
    """Two mappings with the same pair name on one device share one journal and one record set
    while scanning two different directories — every sweep contradicts the last, permanently. All
    three add flows refuse the duplicate by name."""

    def test_every_add_flow_refuses_a_duplicate_name(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sync = open(os.path.join(root, "static", "js", "client", "sync.js"), encoding="utf-8").read()
        self.assertEqual(sync.count("is already syncing on this"), 3,
                         "an add flow lost its duplicate-pair guard")


class FoldingDiskCapability(unittest.TestCase):
    """The desktop bridge tells the engine whether this filesystem folds case, and which platform
    it is — the collision and impossible-name rules decide with it. Absent flags default SAFE
    (fold: report the twin rather than flip-flop)."""

    def test_the_bridge_exports_the_capabilities(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        br = open(os.path.join(root, "desktop", "fsbridge.js"), encoding="utf-8").read()
        self.assertIn("caseFolds: process.platform !== 'linux'", br)
        self.assertIn("platform: process.platform", br)

    def test_the_bridge_heals_an_unreadable_file_itself(self):
        """"I will not tell a user to run terminal commands" — the app clears the read-only bits
        and resets the ACL itself, once per path, and only then gives up and names the file."""
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        br = open(os.path.join(root, "desktop", "fsbridge.js"), encoding="utf-8").read()
        self.assertIn("function healRead(", br)
        self.assertIn("icacls", br)
        self.assertIn("_healed", br, "an unhealable file must be attempted once, not every sweep")
