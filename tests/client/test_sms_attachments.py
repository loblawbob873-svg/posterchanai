"""Picture messages in the Texts screen: what is shown, what is published, and what is deleted.

Run: venv-unified/bin/python -m pytest tests/client/test_sms_attachments.py

These drive the SHIPPED static/js/client/sms.js under node against a stub phone whose message store
holds BOTH kinds (sms_sim.js), because every rule here is a relationship between calls rather than a
string:

  * A DELETE IS TWO DELETES AND NOW TWO URIs. A picture message is a row in `content://mms`; sent
    down the SMS path the provider removes nothing and reports nothing, which the client correctly
    reads as a refusal — so the archive is left alone and the delete quietly did not happen.
  * THE ARCHIVE NAMES ATTACHMENTS AND MUST NOT CLAIM TO HOLD THEM. The bytes are on the handset. A
    laptop that says "2 photos, on your phone" is right; one that draws a broken image is not, and
    one that carries the handset's provider row id is carrying a number that means something else
    on every other device.
  * A REFUSAL OF ONE TABLE IS NOT AN EMPTY INBOX. Several OEM builds guard the MMS tables separately,
    so a phone whose texts read perfectly can hand over no pictures at all — and a thread that
    silently lost its photos has nothing on screen to notice.

Each of these was checked to fail with its rule removed.
"""
import json
import shutil
import subprocess
import time
from pathlib import Path
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests" / "client" / "sms_sim.js"

NOW = int(time.time() * 1000)


def text(n, *, addr="+15550100", body=None, date=None, incoming=True):
    return {"id": n, "thread": 1, "address": addr,
            "body": body if body is not None else "message %d" % n,
            "date": date if date is not None else NOW - 60000 + n * 1000,
            "type": 1 if incoming else 2, "incoming": incoming, "read": False,
            "mms": False, "parts": [],
            "doc": "pcai:sms:%024d" % n}


def picture(n, *, addr="+15550100", body="", date=None, incoming=True, parts=None):
    return {"id": n, "thread": 1, "address": addr, "body": body,
            "date": date if date is not None else NOW - 60000 + n * 1000,
            "type": 1 if incoming else 2, "incoming": incoming, "read": False,
            "mms": True,
            "parts": parts if parts is not None
                     else [{"id": 900 + n, "ct": "image/jpeg", "name": "p%d.jpg" % n,
                            "bytes": 1234}],
            "doc": "pcai:sms:%024d" % n}


def run(**opts):
    out = subprocess.run(["node", str(SIM), json.dumps(opts)], capture_output=True, timeout=90)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-3000:])
    return json.loads(out.stdout.decode())


def calls_of(res, name):
    return [c for c in res["calls"] if c[0] == name]


def published(res, doc):
    for p in res["published"]:
        if p["d"] == doc:
            body = json.loads(p["content"][4:]) if p["content"].startswith("enc:") else None
            if body and body.get("blob"):
                stored = res.get("uploads", {}).get(body["blob"])
                return json.loads(stored["text"]) if stored else None
            return body
    return None


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class ThreadsHoldBothKinds(unittest.TestCase):

    def test_remote_send_keeps_the_uploaded_attachment_in_pending_and_ack_bubbles(self):
        src = Path(ROOT, "static/js/client/sms.js").read_text()
        self.assertIn("const sentParts=sent.attachment?", src)
        self.assertIn("parts:sentParts, pending:true", src)
        self.assertIn("parts:sentParts, _at:ev.created_at", src)
        self.assertIn("parts:pendingParts", src)

    def test_texts_and_pictures_are_one_conversation(self):
        """A conversation is texts AND pictures and has always been read as one thing. Two threads
        for one person is a conversation nobody can follow."""
        res = run(rows=[text(1), picture(2), text(3)], steps=["load", "render", "settle"])
        self.assertEqual(len(res["threads"]), 1, res["threads"])
        self.assertEqual(res["threads"][0]["order"],
                         ["pcai:sms:%024d" % 1, "pcai:sms:%024d" % 2, "pcai:sms:%024d" % 3],
                         "the two kinds did not interleave by date")

    def test_a_picture_message_keeps_its_attachments_through_the_client(self):
        """`parts` dropped anywhere between the plugin and the thread is a blank bubble, which is
        what a message that FAILED looks like."""
        res = run(rows=[picture(2)], steps=["load", "render", "settle"])
        self.assertEqual(res["threads"][0]["parts"], [1])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class TheArchive(unittest.TestCase):

    def test_existing_sms_high_water_mark_does_not_skip_the_blossom_migration(self):
        """The ordering repair shipped before encrypted Blossom storage. Phones therefore already
        carry its marker and a high-water mark at today. Reusing that marker for the storage move
        makes the provider query start at today, returns no historical rows, and leaves Blossom at
        zero files forever on the exact accounts being migrated."""
        mark = NOW + 60000
        res = run(rows=[text(1), picture(2)], steps=["mirror"], storage={
            "pc_sms_hwm_me": mark,
            "pc_sms_hwm_me_oldest_first_v1": "1",
        })
        self.assertTrue(any(f["folder"] == "Messages" for f in res["drive"]["files"]),
                        "the existing high-water mark skipped the message-body migration")
        self.assertTrue(any(f["folder"] == "MMS" for f in res["drive"]["files"]),
                        "the existing high-water mark skipped the MMS migration")
        self.assertGreater(len(res["published"]), 0)

    def test_the_complete_phone_history_is_migrated_across_bounded_batches(self):
        """A successful recent pass is not a completed migration. 1.0.1512 marked it complete
        after three recent rows on the reporter's phone, leaving every older provider row behind.
        Completion now means the full local provider read has no unsealed row remaining."""
        rows = [text(i, date=NOW - (500 + i) * 86400000) for i in range(1, 66)]
        res = run(rows=rows, migrationBatch=60,
                  storage={"pc_sms_hwm_me": NOW, "pc_sms_hwm_me_blossom_v1": "1"},
                  steps=["phoneLoad", "migrate", "migrate"])
        bodies = [f for f in res["drive"]["files"] if f["folder"] == "Messages"]
        self.assertEqual(len(bodies), 65, "older provider rows were left outside Blossom")
        self.assertEqual(len(res["published"]), 65)

    def test_one_unreadable_mms_does_not_block_every_message_after_it(self):
        """The reporter consistently got three files because row four was an MMS whose provider
        part could not cross the bridge. The old loop broke there on every run, so rows five onward
        were unreachable forever. The bad MMS must remain pending without making the good rows
        behind it collateral damage."""
        rows = [text(i, date=NOW - (20 - i) * 1000) for i in range(1, 11)]
        rows[3] = picture(4, date=rows[3]["date"],
                          parts=[{"id": 904, "ct": "image/jpeg", "name": "blocked.jpg",
                                  "bytes": 20 * 1024 * 1024}])
        res = run(rows=rows, parts={"904": {"tooBig": True}}, migrationBatch=60,
                  steps=["phoneLoad", "migrate"])
        bodies = [f for f in res["drive"]["files"] if f["folder"] == "Messages"]
        self.assertEqual(len(bodies), 9, "the unreadable fourth row blocked later messages")
        self.assertEqual(len(res["published"]), 9)
        self.assertIsNone(published(res, rows[3]["doc"]), "a hollow MMS was published")

    def test_one_unreadable_mms_does_not_wall_off_the_ordinary_sweep(self):
        """THE SAME FAULT AS THE MIGRATION'S, IN THE PATH THAT ACTUALLY RUNS.

        The provider answers a `since` query OLDEST FIRST, so a row the loop stops at is in front of
        everything newer than it — and the sweep restarts before that row every time, by design,
        because the mark must stay behind a message that did not land. `break` therefore was not a
        pause: one picture message whose bytes the provider would not hand over froze the entire
        archive at that date, texts included, on every sweep for ever. The photos never reached
        Blossom because nothing after that row was ever offered to it."""
        rows = [text(i, date=NOW - (20 - i) * 1000) for i in range(1, 11)]
        rows[3] = picture(4, date=rows[3]["date"],
                          parts=[{"id": 904, "ct": "image/jpeg", "name": "blocked.jpg",
                                  "bytes": 20 * 1024 * 1024}])
        res = run(rows=rows, parts={"904": {"tooBig": True}}, steps=["mirror", "mirror"])
        landed = {p["d"] for p in res["published"]}
        for r in rows:
            if r is rows[3]:
                continue
            self.assertIn(r["doc"], landed,
                          "the unreadable picture message walled off the ordinary sweep")
        self.assertNotIn(rows[3]["doc"], landed, "a hollow MMS was published")

    def test_the_high_water_mark_is_rewound_once_and_not_on_every_sweep(self):
        """The rewind was keyed on the MIGRATION being finished rather than on having rewound. Any
        phone the migration cannot finish on — one attachment the provider refuses is enough — had
        its mark dragged back to the thirty-day boundary on every single sweep, so it republished
        the same month for ever and never moved forward."""
        rows = [text(i, date=NOW - (20 - i) * 1000) for i in range(1, 6)]
        res = run(rows=rows, storage={"pc_sms_hwm_me": NOW + 60000,
                                      "pc_sms_hwm_me_oldest_first_v1": "1"},
                  steps=["mirror", "mirror"])
        asked = [c[1] for c in calls_of(res, "list")]
        self.assertEqual(len(asked), 2, asked)
        # The first sweep rewinds to the documented first-run boundary; the second must start from
        # where the first finished. Compared against the boundary rather than against each other,
        # because two rewinds a millisecond apart are two rewinds.
        self.assertLess(asked[0], NOW - 29 * 86400000, "the first sweep did not rewind at all")
        self.assertGreater(asked[1], NOW - 29 * 86400000,
                           "the second sweep was dragged back to the boundary again")

    def test_a_picture_with_no_usable_preview_is_archived_once_not_for_ever(self):
        """A thumbnail is a bandwidth saving, not part of being archived. Read as a missing piece,
        an image the WebView cannot decode was never `done`: republished on every migration batch,
        blocking the completion marker, and so dragging the mark back on every sweep behind it. Node
        has no `createImageBitmap`, which is exactly the shape of that failure."""
        rows = [text(1), picture(2), picture(3)]
        res = run(rows=rows, storage={"pc_sms_hwm_me": NOW + 60000,
                                      "pc_sms_hwm_me_oldest_first_v1": "1"},
                  steps=["phoneLoad", "migrate", "migrate", "migrate"])
        seen = [p["d"] for p in res["published"]]
        self.assertEqual(len(seen), len(set(seen)),
                         "a picture with no preview was published again on every pass: %r" % seen)
        body = published(res, rows[1]["doc"])
        self.assertRegex(body["att"][0]["sha"], r"^[0-9a-f]{64}$",
                         "the original was not archived")
        self.assertEqual(body["att"][0].get("nt"), 1,
                         "the impossible preview was not recorded, so it will be retried for ever")

    def test_the_migration_loop_is_bounded_by_progress_not_by_its_safety_limit(self):
        """Every batch PULLS and SAVES the encrypted file index. A queue that has stopped shrinking
        used to run the full thousand-pass safety limit — a thousand rewrites of a replaceable
        document for one opening of the Texts screen."""
        # MORE PICTURES THAN ONE BATCH HOLDS, so a queue that stops shrinking still reports rows
        # remaining on every pass — the state the old loop answered by running to its safety limit.
        rows = [picture(100 + i, date=NOW - (100 + i) * 86400000) for i in range(1, 71)]
        res = run(rows=rows, migrationBatch=60,
                  storage={"pc_sms_hwm_me": NOW, "pc_sms_hwm_me_oldest_first_v1": "1"},
                  steps=["phoneLoad", "migrateAll"])
        self.assertLessEqual(len(calls_of(res, "drivePull")), 4,
                             "the migration kept re-opening the drive after it stopped progressing")
        self.assertEqual(len([f for f in res["drive"]["files"] if f["folder"] == "MMS"]), 70,
                         "not every picture reached the encrypted MMS folder")

    def test_a_truncated_picture_table_cannot_complete_the_migration(self):
        """`MmsStore.MAX_ROWS` hands back the newest 2,000 picture messages and there is no way to
        ask for the rest, so past the ceiling the oldest ones are not in the local set AT ALL. The
        candidate queue is then empty for the honest reason that nothing asked for them — and every
        other term in the completion test is about whether the rows we were GIVEN landed, so the
        migration marked itself done, never ran again, and the screen said it had copied the phone.
        Truncated is not exhausted, the same rule `refused` draws one step along."""
        res = run(rows=[text(1), picture(2)], mmsCapped=True, migrationBatch=60,
                  storage={"pc_sms_hwm_me": NOW + 60000,
                           "pc_sms_hwm_me_oldest_first_v1": "1"},
                  steps=["phoneLoad", "migrateAll"])
        self.assertTrue(res["mmsCapped"], "the client dropped the picture-table ceiling")
        self.assertFalse(res["blossomDone"],
                         "a truncated provider read was recorded as a completed migration")

    def test_an_untruncated_read_still_completes(self):
        """The other half: a ceiling that is never reached must not leave every phone permanently
        mid-migration, re-walking its whole history on every visit."""
        res = run(rows=[text(1), picture(2)], migrationBatch=60,
                  storage={"pc_sms_hwm_me": NOW + 60000,
                           "pc_sms_hwm_me_oldest_first_v1": "1"},
                  steps=["phoneLoad", "migrateAll"])
        self.assertFalse(res["mmsCapped"])
        self.assertTrue(res["blossomDone"], "an ordinary phone never finishes migrating")

    def test_body_and_picture_are_committed_to_encrypted_drive_folders(self):
        """A successful relay event is not enough: other devices find bytes through FilesIdx. The
        transaction must persist both folders before Android is free to freeze the WebView."""
        res = run(rows=[picture(2)], steps=["load", "mirror"])
        self.assertEqual(res["drive"]["batch"], 0, "the drive transaction was left open")
        self.assertIn("Messages", res["drive"]["folders"])
        self.assertIn("MMS", res["drive"]["folders"])
        self.assertTrue(any(f["folder"] == "Messages" for f in res["drive"]["files"]),
                        "the encrypted message body is not indexed")
        self.assertTrue(any(f["folder"] == "MMS" for f in res["drive"]["files"]),
                        "the MMS original/preview is not indexed")
        self.assertTrue(calls_of(res, "driveEnd"), "FilesIdx was never durably committed")

    def test_native_attachment_chunks_are_reassembled_before_encrypted_upload(self):
        """The Android bridge returns large MMS files in bounded pieces. Uploading one piece or
        stopping after the first successful reply creates a valid encrypted file with silently
        truncated media, which a thumbnail test alone would not expose."""
        raw = b"one attachment, across several bridge replies"
        row = picture(4, parts=[{"id": 904, "ct": "image/jpeg", "name": "whole.jpg",
                                  "bytes": len(raw)}])
        res = run(rows=[row], parts={"904": {"data": __import__("base64").b64encode(raw).decode()}},
                  chunked=True, chunkSize=7, steps=["mirror"])
        mms = [v for v in res["uploads"].values() if v["folder"] == "MMS"
               and v["name"] == "whole.jpg"]
        self.assertEqual(len(mms), 1)
        self.assertEqual(mms[0]["text"], raw.decode())
        self.assertGreater(len(calls_of(res, "attachment")), 1)

    def test_it_names_the_attachments_and_does_not_claim_to_hold_them(self):
        """A laptop that knows a message carried a photo can say where it is. What it must not do is
        draw an empty bubble — and what it must not carry is the handset's provider row id, which
        means something different on every other device (the same reason a message's address is
        derived from the message and never from its row)."""
        res = run(rows=[picture(2)], steps=["load", "mirror"])
        body = published(res, "pcai:sms:%024d" % 2)
        self.assertIsNotNone(body, "the picture message was never published")
        self.assertTrue(body.get("mms"), "the archive does not record that it was a picture message")
        self.assertEqual(len(body.get("att") or []), 1)
        att = body["att"][0]
        self.assertEqual(att["ct"], "image/jpeg")
        self.assertRegex(att["sha"], r"^[0-9a-f]{64}$")
        self.assertNotIn("id", att, "the handset's provider row id was published")

    def test_an_ordinary_text_gains_nothing(self):
        """The `att`/`mms` keys must not appear on a plain text, or every message in a long history
        is republished a byte larger for no reason and the two halves' documents stop matching."""
        res = run(rows=[text(1)], steps=["load", "mirror"])
        body = published(res, "pcai:sms:%024d" % 1)
        self.assertNotIn("att", body)
        self.assertNotIn("mms", body)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class Deleting(unittest.TestCase):

    def test_a_picture_message_is_deleted_down_the_mms_path(self):
        """THE FAILURE IS SILENT IN BOTH DIRECTIONS. Sent down the SMS path the provider removes
        nothing and reports nothing; the client reads 0 rows as a refusal and — correctly — leaves
        the archive alone. So the message stays on the phone AND in the archive, the toast says
        nothing was changed, and the bubble is still there after the next repaint."""
        doc = "pcai:sms:%024d" % 2
        res = run(rows=[text(1), picture(2)], steps=["load", "remove:" + doc])
        sent = calls_of(res, "delete")
        self.assertTrue(sent, "nothing was deleted on the phone")
        self.assertEqual(sent[0][1], [], "a picture message was sent down the text path")
        self.assertEqual(sent[0][2], [2], "the mms row id never reached the provider")
        self.assertEqual(calls_of(res, "removeResult")[0][1:], [1, 1],
                         "the delete did not remove both copies")

    def test_a_text_still_goes_down_the_text_path(self):
        doc = "pcai:sms:%024d" % 1
        res = run(rows=[text(1), picture(2)], steps=["load", "remove:" + doc])
        sent = calls_of(res, "delete")
        self.assertEqual(sent[0][1], [1])
        self.assertEqual(sent[0][2], [])


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class RefusalIsNotEmptiness(unittest.TestCase):

    def test_a_picture_table_that_would_not_answer_is_said_out_loud(self):
        """The texts are on screen and complete; every photo is missing. There is nothing on the
        screen to notice — it reads as a conversation somebody sent fewer photos in. Folded into the
        one `refused` flag it would instead blame the whole screen, over a full inbox, which is the
        exact report this screen was rebuilt for."""
        res = run(rows=[text(1)], mmsRefused=True, steps=["load", "render", "settle"])
        self.assertTrue(res["mmsRefused"], "the client dropped the picture-table refusal")

    def test_an_ordinary_read_does_not_claim_a_refusal(self):
        res = run(rows=[text(1), picture(2)], steps=["load", "render", "settle"])
        self.assertFalse(res["mmsRefused"])


if __name__ == "__main__":
    unittest.main()
