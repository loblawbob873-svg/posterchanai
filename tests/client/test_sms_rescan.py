"""THE WAY BACK OUT OF "ALREADY DONE".

Every marker the message archive keeps is a LATCH, and the completion one — `..._blossom_v9` — is
read at the top of `migrateLocalHistory` and returns immediately when it is set. That is correct
when it was set correctly. When it was set by an older build that believed a truncated or walled-off
read was the whole phone, it is permanent: the device installs every fix that follows, opens Texts,
sees a full screen of messages and a migration that does nothing, and there is nothing on screen to
say why. Every fix so far has been to the code that DECIDES to set the latch; none of them reaches a
device that already did.

Reported as "I don't see all my SMS messages synced, the app on phone shows way more" and "no media
from the texts are showing up on the webui" — from a handset whose own Texts screen was perfect,
because on the phone an attachment is read straight from `content://mms/part` and needs no archive
at all. The web has no provider, so it renders what reached Blossom, and nothing had.

Each check here was verified to fail with the rule removed.

Run: venv-unified/bin/python -m pytest tests/client/test_sms_rescan.py
"""
import json
import os
import re
import shutil
import subprocess
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "sms_sim.js")
NODE = shutil.which("node")

# The REAL clock: the client rewinds against Date.now(), so a fictional epoch puts every row on
# the far side of the first-run boundary and the assertions compare against the wrong scale.
NOW = int(time.time() * 1000)
ME = "pc_sms_hwm_me"


def picture(i, date):
    # `doc` is what SmsPlugin computes on a real handset — the sim carries it the same way and the
    # client never derives it, so a row without one is silently dropped by loadFromPhone.
    return {"id": i, "thread": 1, "address": "+15550100", "body": "", "date": date,
            "type": 1, "incoming": True, "read": True, "mms": True,
            "parts": [{"id": 900 + i, "ct": "image/jpeg", "name": "p%d.jpg" % i, "bytes": 2048}],
            "doc": "pcai:sms:%024d" % i}


def run(rows, storage, steps, extra=None):
    # Options ride on argv[2], not stdin — see sms_sim.js's usage line.
    payload = {"rows": rows, "storage": storage, "steps": steps, "now": NOW, "canRead": True}
    if extra:
        payload.update(extra)
    r = subprocess.run([NODE, SIM, json.dumps(payload)], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr[-4000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def mms_files(res):
    return [f for f in res["drive"]["files"] if f["folder"] == "MMS"]


@unittest.skipIf(not NODE, "no node on this node")

def _current_blossom_latch():
    """The completion latch the SHIPPED code reads, not a version typed in here.

    This test file pins several OLD versions on purpose — each one is a device state somebody was
    actually stuck in — but the "already done, so do nothing" case must always mean the CURRENT
    marker. Typed in, it silently stops testing anything the moment the latch is bumped, which is
    exactly what happened at v10: the assertion still passed a phone that migrates nothing because
    it was pinning a key the code no longer reads."""
    src = open(os.path.join(ROOT, "static", "js", "client", "sms.js"), encoding="utf-8").read()
    m = re.search(r"HWM_BLOSSOM = \(\) => HWM\(\) \+ '([^']+)'", src)
    assert m, "the completion latch moved and this test stopped checking anything"
    return m.group(1)

class AStuckCompletionMarker(unittest.TestCase):

    def test_v7_reaudits_mms_through_the_dedicated_provider_pager(self):
        """Representative Android provider rows: SMS history can be complete while the combined
        timeline omitted old MMS. The independent provider walk must still upload each part and
        publish a portable sha for Web/OS, even when the prior release marked migration complete."""
        rows = [picture(i, NOW - (1000 + i) * 60000) for i in range(1, 406)]
        # BOUNDED PER VISIT, AND IT MUST STILL CONVERGE. A visit copies ~120 rows and stops: each
        # row is an encrypted upload AND a relay write, and a handset doing hundreds of those is
        # not a phone anybody can read a text on — reported as Texts glitching. So the contract is
        # no longer "one call finishes the history", it is "every call makes progress and the
        # history completes over several". Driven here the way a person drives it: open Texts,
        # again, and again.
        res = run(rows, {ME + "_blossom_v7": "1", ME: str(NOW)},
                  ["phoneLoad"] + ["migrateAll"] * 12, {"combinedOmitsMms": True})
        # A VISIT COPIES TEN ROWS — a row can be a 12 MB picture now, so the sweep is deliberately
        # small. What this test is about is the PAGER: that the independent MMS walk crosses its
        # first page and that everything it finds is archived WITH a portable sha. Convergence over
        # many visits is proved by TheMigrationIsBoundedPerVisit; here we assert steady progress and
        # that nothing arrives without its media address.
        self.assertGreaterEqual(len(mms_files(res)), 100,
                                "the migration made almost no progress across repeated visits")
        self.assertGreaterEqual(sum(1 for c in res["calls"] if c[0] == "listMms"), 2,
                                "MMS-only strict-before paging did not cross its first page")
        # Not every message is copied in one visit any more, so the rule is about the ones that
        # WERE: each has a portable address, and every uploaded file is accounted for by one.
        part_shas = [sha for thread in res["threads"] for row in thread["partShas"] for sha in row]
        filled = [sha for sha in part_shas if sha]
        self.assertEqual(len(filled), len(mms_files(res)),
                         "an uploaded file is not referenced by any archived part, or the other "
                         "way round")
        self.assertTrue(filled, "no old provider part reached the archive with a media sha")

    def test_v5_completion_is_reaudited_through_the_fixed_history_pager(self):
        """The history pager was repaired after v5 shipped. Keeping that old latch makes the fix
        unreachable on precisely the established phones that need it: live MMS continues to mirror,
        while media behind the already-complete first page is never revisited."""
        rows = [picture(i, NOW - (100 + i) * 86400000) for i in range(1, 4)]
        res = run(rows, {ME + "_blossom_v5": "1", ME + "_oldest_first_v1": "1",
                         ME: str(NOW)}, ["phoneLoad", "migrateAll"])
        self.assertEqual(len(mms_files(res)), 3,
                         "the obsolete v5 latch still hid historical MMS media")
        self.assertTrue(res["blossomDone"], "the current audit did not record its own completion")

    def test_backfill_archives_media_already_visible_from_the_phone(self):
        """Provider rows are loaded into the screen first. They are not thereby archived: a photo
        still needs encrypted Blossom hashes, and backfill must revisit rather than cursor past it."""
        rows = [picture(1, NOW - 5 * 86400000)]
        res = run(rows, {}, ["phoneLoad", "importAll"])
        self.assertEqual(len(mms_files(res)), 1,
                         "the visible MMS row was skipped instead of uploading its media")

    def test_dense_old_history_pages_without_skipping_a_window(self):
        """More than 400 messages in one 90-day span used to lose the middle permanently: the
        client selected the oldest page in the span, then jumped its window behind that page."""
        rows = []
        for i in range(405):
            rows.append({"id": i + 1, "thread": 1, "address": "+15550100",
                         "body": "old-%d" % i, "date": NOW - (i + 1) * 60000,
                         "type": 1, "incoming": True, "read": True, "mms": False,
                         "parts": [], "doc": "pcai:sms:dense%019d" % i})
        res = run(rows, {}, ["importAll"])
        archived = [d for d in res["docs"] if d.startswith("pcai:sms:dense")]
        self.assertEqual(len(archived), 405,
                         "strict backward paging skipped part of a dense history window")

    def test_phone_load_pages_past_the_mms_provider_ceiling(self):
        """A large `recent` request is still capped by Android's MMS table. The old 1k/10k/50k
        growth loop stopped at the 2k answer and older media never entered the migration set."""
        res = run([], {}, ["phoneLoad"], {"providerPageCap": 2000,
                                          "generatedPictures": 2105})
        self.assertEqual(len(res["docs"]), 2105,
                         "phone history stopped at the MMS provider's first-page ceiling")
        before = [c[2] for c in res["calls"] if c[0] == "list"]
        self.assertGreater(len(before), 5, "history was not read through bounded before-pages")
        self.assertTrue(all(v > 0 for v in before), "a history page did not carry a strict cursor")
        self.assertFalse(res["mmsCapped"], "an exhausted paged read was reported as truncated")

    def test_v6_completion_is_reaudited_by_the_paged_provider_reader(self):
        """v6 could mark the newest capped MMS slice complete. That latch must not make the new
        provider pager unreachable on precisely the established phones missing old desktop media."""
        rows = [picture(i, NOW - i * 60000) for i in range(1, 6)]
        res = run(rows, {ME + "_blossom_v6": "1"}, ["phoneLoad", "migrateAll"])
        self.assertEqual(len(mms_files(res)), 5, "the obsolete v6 latch hid old MMS again")
        self.assertTrue(res["blossomDone"])

    def test_completed_first_page_does_not_hide_older_mms_media(self):
        """A second backfill normally starts over already-archived recent rows. Its provider
        cursor must cross that quiet page to discover an old picture added behind the boundary."""
        recent = [{"id": i + 1, "thread": 1, "address": "+15550100",
                   "body": "recent-%d" % i, "date": NOW - (i + 1) * 60000,
                   "type": 1, "incoming": True, "read": True, "mms": False,
                   "parts": [], "doc": "pcai:sms:recent%018d" % i}
                  for i in range(400)]
        old_picture = picture(5000, NOW - 500 * 60000)
        res = run(recent, {}, ["importAll", "appendRows", "importAll"],
                  {"appendRows": [old_picture]})
        self.assertEqual(len(mms_files(res)), 1,
                         "an already-complete first page stopped paging before the old MMS")

    def test_an_old_v2_completion_latch_cannot_hide_mms_after_upgrade(self):
        """The release migration itself: v2 could declare success with no portable MMS hashes.
        A current build must regard that old latch as stale and perform one full audit automatically."""
        rows = [picture(i, NOW - (100 + i) * 86400000) for i in range(1, 5)]
        res = run(rows, {ME + "_blossom_v2": "1", ME + "_blossom_rewound_v2": "1",
                         ME + "_oldest_first_v1": "1", ME: str(NOW)},
                  ["phoneLoad", "migrateAll"])
        self.assertEqual(len(mms_files(res)), 4,
                         "the obsolete completion latch still suppresses the repaired MMS audit")
        self.assertTrue(res["blossomDone"], "the replacement v3 migration did not finish")

    def test_a_phone_already_marked_done_migrates_nothing(self):
        """THE BUG, stated as the thing the user sees. The latch is set, so the whole history is
        skipped and not one picture reaches encrypted storage — with no error anywhere."""
        rows = [picture(i, NOW - (100 + i) * 86400000) for i in range(1, 6)]
        res = run(rows, {ME + _current_blossom_latch(): "1", ME + "_oldest_first_v1": "1",
                         ME: str(NOW)}, ["phoneLoad", "migrateAll"])
        self.assertEqual(mms_files(res), [],
                         "the latch did not actually block the migration — this test proves nothing")


    def test_v9_done_is_reaudited_because_v9_archived_no_attachments(self):
        """WHY v10 EXISTS. Every phone that reached "done" under v9 published an archive with NO
        ATTACHMENTS IN IT — `withMmsParts` was called on the live branch only, so the back-fill read
        bare rows and filed picture messages carrying no `att` key at all. Measured on the reporting
        account: 2,676 documents, 1,775 flagged `mms:true`, not one with an attachment.

        Those devices are the ones that need the fix most and are exactly the ones that return early
        on the old marker. Without the bump the repair underneath them can never run, and there is
        nothing on screen to say so."""
        rows = [picture(i, NOW - (100 + i) * 86400000) for i in range(1, 4)]
        res = run(rows, {ME + "_blossom_v9": "1", ME: str(NOW)}, ["phoneLoad", "migrateAll"])
        self.assertEqual(len(mms_files(res)), 3,
                         "a phone marked done by v9 — with a pictureless archive — was left alone")

    def test_v8_done_is_reaudited_and_old_apk_cannot_write_v9_done(self):
        """The combined provider can omit old MMS. Only the independent listMms walk proves them."""
        rows = [picture(i, NOW - (100 + i) * 86400000) for i in range(1, 4)]
        repaired = run(rows, {ME + "_blossom_v8": "1", ME: str(NOW)},
                       ["phoneLoad", "migrateAll"])
        self.assertEqual(len(mms_files(repaired)), 3)
        self.assertTrue(repaired["blossomDone"])

        old = run(rows, {}, ["phoneLoad", "migrateAll"], {"oldApk": True})
        self.assertFalse(old["blossomDone"],
                         "an APK with no independent MMS audit permanently hid historical media")

    def test_a_rescan_clears_the_latch_and_copies_the_whole_phone(self):
        """The way out. Same phone, same latch, one deliberate re-scan."""
        rows = [picture(i, NOW - (100 + i) * 86400000) for i in range(1, 6)]
        res = run(rows, {ME + "_blossom_v7": "1", ME + "_oldest_first_v1": "1", ME: str(NOW)},
                  ["phoneLoad", "rescan"])
        self.assertEqual(len(mms_files(res)), 5,
                         "the re-scan did not reach the pictures the latch was hiding")

    def test_the_rescan_also_clears_the_high_water_mark(self):
        """Clearing the completion flag alone is not enough: the MARK is what makes the ordinary
        sweep start at `now` rather than at the beginning, so a re-scan that left it would re-run a
        migration that still could not reach anything old."""
        rows = [picture(i, NOW - (100 + i) * 86400000) for i in range(1, 4)]
        res = run(rows, {ME + "_blossom_v7": "1", ME + "_oldest_first_v1": "1", ME: str(NOW)},
                  ["phoneLoad", "rescan"])
        # Behind the first-run boundary, which is what lets the ordinary sweep reach back at all.
        self.assertLess(res["hwm"], NOW - 20 * 86400000,
                        "the mark still sits near 'now', so nothing older can ever be swept")

    def test_a_rescan_is_safe_to_repeat(self):
        """It is a button somebody will press twice. The second pass must not duplicate the
        uploads — the archive is keyed on the message, not on when it was read."""
        rows = [picture(i, NOW - (100 + i) * 86400000) for i in range(1, 4)]
        once = run(rows, {ME + "_blossom_v7": "1"}, ["phoneLoad", "rescan"])
        twice = run(rows, {ME + "_blossom_v7": "1"}, ["phoneLoad", "rescan", "rescan"])
        self.assertEqual(len(mms_files(once)), 3)
        self.assertEqual(len(mms_files(twice)), len(mms_files(once)),
                         "a second re-scan re-uploaded everything")

    def test_foreground_resumes_an_interrupted_complete_history_migration(self):
        """The ordinary foreground path used to run only mirror(), whose high-water cursor moves
        forward. Once an old-history upload was interrupted, returning online could sync new texts
        forever while the older tail stayed absent forever."""
        rows = [picture(i, NOW - (100 + i) * 86400000) for i in range(1, 6)]
        # `load` models that Texts has actually been opened. Production deliberately ignores a
        # generic browser focus event while the Texts archive is still cold; calling only the
        # exported migration helpers left S.ready false and tested the startup guard, not resume.
        payload = {"rows": rows, "storage": {}, "steps":
                   ["load", "phoneLoad", "migrateAll", "allow", "foreground"],
                   "now": NOW, "canRead": True, "refuseAfter": 0}
        proc = subprocess.run([NODE, SIM, json.dumps(payload)], capture_output=True, text=True,
                              timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])
        result = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(len(mms_files(result)), 5,
                         "foreground sync left the interrupted historical tail behind")
        self.assertTrue(result["blossomDone"],
                        "the resumed full-history migration did not converge")


if __name__ == "__main__":
    unittest.main()
