"""Texts on a device that is NOT the phone: why the screen took minutes, and why the pictures on
the old messages never arrived.

Run: venv-unified/bin/python -m unittest tests.client.test_sms_media_recovery

Everything here drives the SHIPPED static/js/client/sms.js under node (tests/client/sms_sim.js).
The four rules, each measured against a real deployment before it was written and each verified to
fail with its fix removed:

  * THE BROAD RELAY READ IS A REPAIR, NOT THE HOT PATH. `BROAD_FILTER` is author+kind with no `d`
    bound, and kind 30078 is this app's entire datastore. Counted on the production relay for one
    account: 19,480 `pcai:fs` folder-sync records and 17,805 `pcai:mail` rows against 4,619
    `pcai:sms` — so sending it on every open, every focus and every lifecycle resume downloads the
    account to paint a screen. It runs once per session, behind the first paint.
  * OPENING THE ARCHIVE IS FANNED OUT. Every one of those 4,619 documents is a Blossom pointer, so
    opening one costs a signer round trip plus a fetch plus a decrypt, and they were paid strictly
    one at a time. With a browser extension holding the key that is minutes of spinner.
  * A PREVIEW THAT CANNOT BE READ MUST NOT LOSE THE PICTURE. The thumbnail is a separate blob with
    its own life; read as the attachment's only address, one missing preview hides an original that
    is sitting there intact — and it hides it on the OLDEST messages first, because those are the
    ones whose thumbnails were written by the oldest builds.
  * ONE REPAINT MUST NOT ABANDON EVERY ATTACHMENT BEHIND THE ONE IN FLIGHT. Hydration RETURNED the
    moment its current element left the document, and every repaint of the same conversation does
    that: a keystroke, a receipt, a live event, and the cold-load drain after each batch. A thread
    with more pictures than fitted between two repaints never reached its tail at all.
"""
import hashlib
import json
import shutil
import subprocess
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests" / "client" / "sms_sim.js"

NOW = int(time.time() * 1000)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def ev(d, payload, at=1000, labelled=True):
    tags = [["d", d], ["l", "pcai-sms"]] if labelled else [["d", d]]
    return {"kind": 30078, "content": "enc:" + json.dumps(payload), "created_at": at,
            "pubkey": "me", "id": "x" + d, "tags": tags}


def blob_ev(d, payload, at=1000):
    """An archive row IN THE SHAPE THIS DEPLOYMENT ACTUALLY HAS. Measured on the production relay:
    every one of the 4,619 `pcai:sms:` events is 220-388 bytes of ciphertext, i.e. a Blossom
    pointer — the inline-body form is gone. A fixture that inlines the body exercises neither the
    fetch nor the envelope cache."""
    text = json.dumps(payload)
    digest = hashlib.sha256(text.encode()).hexdigest()
    upload = {digest: {"folder": "Messages", "name": "m.json", "type": "application/json",
                       "text": text}}
    return (ev(d, {"v": 1, "blob": digest, "mime": "application/json"}, at), upload)


def blob_archive(n, labelled=True):
    events, uploads = [], {}
    for i in range(n):
        e, u = blob_ev("pcai:sms:%03d" % i, body(i))
        events.append(e)
        uploads.update(u)
    return events, uploads


def body(n, **extra):
    out = {"address": "+15550100", "body": "message %d" % n, "date": NOW - n * 1000,
           "incoming": True}
    out.update(extra)
    return out


def run(**opts):
    out = subprocess.run(["node", str(SIM), json.dumps(opts)], capture_output=True, timeout=120)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-4000:])
    return json.loads(out.stdout.decode())


def calls_of(res, name):
    return [c for c in res["calls"] if c[0] == name]


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class TheRelayIsNotAskedForTheWholeDatastore(unittest.TestCase):
    def test_the_hot_path_relay_read_is_the_indexed_one(self):
        """Load, then a lifecycle resume. The label filter may go out as often as it likes; the
        unbounded one is a session-scoped repair and must go out once."""
        archive = [ev("pcai:sms:%03d" % n, body(n)) for n in range(6)]
        res = run(isPhone=False, cached=[], relay=archive, realArchiveFilters=True,
                  steps=["load", "settle", "foreground", "settle"])
        asked = [c[1] for c in calls_of(res, "relayQuery")]
        self.assertEqual(sorted(res["docs"]), sorted(e["tags"][0][1] for e in archive))
        self.assertEqual(asked.count("broad"), 1,
                         "the unbounded kind-30078 read went out more than once: %r" % (asked,))
        self.assertGreaterEqual(asked.count("label"), 1, asked)
        self.assertNotIn("label+broad", asked,
                         "the hot-path relay read still carries the unbounded filter")

    def test_the_session_sweep_still_recovers_an_unlabelled_archive(self):
        """Removing the broad filter from the hot path must not cost the repair it exists for: a
        phone build that did not set `l=pcai-sms` is addressed by `d` alone and has to be found."""
        old = ev("pcai:sms:written-by-an-older-build", body(1, body="still here"), labelled=False)
        res = run(isPhone=False, cached=[], relay=[old], realArchiveFilters=True,
                  steps=["load", "settle"])
        self.assertEqual(res["docs"], ["pcai:sms:written-by-an-older-build"])

    def test_a_sweep_that_could_not_run_is_retried_rather_than_latched(self):
        """`relayDown` is "I could not ask", which is never "there is nothing there" — the rule
        this codebase keeps relearning. The relay is unreachable for the cold load and comes back
        before the resume; the unlabelled archive has to arrive on that second attempt, because a
        flag set in front of the attempt would have spent the account's only repair on a socket
        that was still connecting."""
        old = ev("pcai:sms:older-build", body(1, body="found on the retry"), labelled=False)
        res = run(isPhone=False, cached=[], relay=[old], realArchiveFilters=True,
                  relayDownUntilForeground=True,
                  steps=["load", "settle", "foreground", "settle"])
        self.assertEqual(res["docs"], ["pcai:sms:older-build"],
                         "a sweep that could not run latched itself as done")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class OpeningTheArchiveIsFannedOut(unittest.TestCase):
    def test_documents_are_opened_in_parallel(self):
        """Serial and parallel produce an identical transcript, so the concurrency is measured
        directly. One at a time is what made a real 4,619-message archive minutes of spinner."""
        archive = [ev("pcai:sms:%03d" % n, body(n)) for n in range(24)]
        res = run(isPhone=False, cached=archive, relayEmpty=True, realArchiveFilters=True,
                  decryptDelayAll=5, steps=["load", "settle"])
        self.assertEqual(len(res["docs"]), 24)
        self.assertGreater(res["decryptPeak"], 1,
                           "the archive is still opened one message at a time")
        self.assertLessEqual(res["decryptPeak"], 6,
                             "the fan-out is unbounded — a NIP-07 extension denies the overflow "
                             "with no prompt at all")

    def test_the_cache_drain_and_the_relay_refresh_do_not_open_the_same_document_twice(self):
        """They overlap BY DESIGN — load() detaches the drain over the local cache and then awaits
        a relay refresh, and those are the same documents. Whoever committed second still paid for
        its own signer round trip and its own blob fetch first, which on a real archive is the
        whole thing decrypted twice while somebody waits for the screen."""
        archive = [ev("pcai:sms:%03d" % n, body(n)) for n in range(64)]
        res = run(isPhone=False, cached=archive, relay=archive, realArchiveFilters=True,
                  decryptDelayAll=2, steps=["load", "settle"])
        self.assertEqual(len(res["docs"]), 64)
        self.assertLessEqual(res["decryptCalls"], 64,
                             "the archive was opened more than once per document (%d decrypts for "
                             "64 messages)" % res["decryptCalls"])

    def test_reopening_texts_does_not_ask_the_signer_again(self):
        """THE ENVELOPE IS CACHED, THE BODY IS NOT.

        What `nip44dec` yields for a modern archive row is `{v,blob,mime}` — a content hash and a
        MIME type, not a word of anybody's conversation — and it is the half a browser extension
        gates and nothing can widen. Cached on the immutable event id, a second visit opens the
        archive with no signer round trips at all; the message bodies still come from the encrypted
        drive every time, so nothing readable is written to disk."""
        archive, uploads = blob_archive(20)
        res = run(isPhone=False, cached=archive, relayEmpty=True, realArchiveFilters=True,
                  fakeIndexedDB=True, uploads=uploads,
                  steps=["load", "settle", "forgetArchive", "absorbCached", "settle"])
        self.assertEqual(len(res["docs"]), 20, "the second pass did not rebuild the archive")
        self.assertEqual(res["decryptCalls"], 20,
                         "reopening Texts asked the signer all over again (%d decrypts for two "
                         "passes over 20 messages)" % res["decryptCalls"])

    def test_an_inline_bodied_row_is_never_written_to_disk(self):
        """An envelope that is not a blob pointer IS the message. Older builds published exactly
        that shape, so the writer has to refuse it — and the reader has to refuse it too, or a row
        left by some other build is handed to openMessageBody as though it were a body."""
        js = (ROOT / "static" / "js" / "client" / "sms.js").read_text(encoding="utf-8")
        writer = js.split("async function archiveEnvelope", 1)[1].split("async function openArchiveDoc", 1)[0]
        self.assertIn("/^[0-9a-f]{64}$/i.test(String(env.blob || ''))", writer,
                      "the envelope cache writes something that is not a blob pointer")
        reader = js.split("async function envRead", 1)[1].split("function envWrite", 1)[0]
        self.assertIn("/^[0-9a-f]{64}$/i.test(String(env.blob || ''))", reader,
                      "the envelope cache trusts whatever it finds on disk")

    def test_a_damaged_row_still_stops_at_itself(self):
        """The fan-out carries failures rather than throwing them, so absorb keeps deciding which
        errors are permanent. One unreadable document must cost exactly one document."""
        bad = ev("pcai:sms:bad", body(1))
        bad["content"] = "enc:"
        good = [ev("pcai:sms:%03d" % n, body(n)) for n in range(4)]
        res = run(isPhone=False, cached=[bad] + good, relayEmpty=True, realArchiveFilters=True,
                  rejectInvalidPlaintext=True, steps=["load", "settle"])
        self.assertEqual(sorted(res["docs"]), sorted(e["tags"][0][1] for e in good))


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class PicturesOnADeviceThatIsNotThePhone(unittest.TestCase):
    def test_a_lost_preview_falls_back_to_the_original(self):
        """The original is present; only the thumbnail blob is gone. That must draw the picture."""
        original = sha("the actual photograph")
        res = run(isPhone=False, cached=[], relayEmpty=True,
                  uploads={original: {"folder": "MMS", "name": "p.jpg", "type": "image/jpeg",
                                      "text": "the actual photograph"}},
                  attProbe={"sha": original, "thumb": sha("a preview nobody kept")})
        probe = res["attProbe"]
        self.assertTrue(probe["firstDrawn"],
                        "a missing preview lost a picture whose original was intact: %r" % (probe,))
        self.assertFalse(probe["preview"], "the original was served but still called a preview")
        self.assertEqual(probe["asked"][:2], [sha("a preview nobody kept"), original],
                         "the preview must be tried first and the original second: %r" % (probe,))

    def test_an_attachment_is_read_once_across_repaints(self):
        """`ATT` is keyed on the phone's provider row id, which is 0 for everything that arrived
        through the archive — so on every device the archive exists to serve, nothing was ever
        remembered and each repaint re-fetched and re-decrypted the whole conversation."""
        original = sha("a photo")
        res = run(isPhone=False, cached=[], relayEmpty=True,
                  uploads={original: {"folder": "MMS", "name": "p.jpg", "type": "image/jpeg",
                                      "text": "a photo"}},
                  attProbe={"sha": original})
        probe = res["attProbe"]
        self.assertTrue(probe["firstDrawn"] and probe["secondDrawn"], probe)
        self.assertEqual(probe["asked"], [original],
                         "the same attachment was read from encrypted storage twice: %r" % (probe,))

    def test_an_attachment_with_no_bytes_anywhere_says_so_and_is_not_asked_twice(self):
        res = run(isPhone=False, cached=[], relayEmpty=True, attProbe={"sha": sha("gone")})
        probe = res["attProbe"]
        self.assertFalse(probe["firstDrawn"])
        self.assertIn("encrypted storage", probe["firstWhy"])
        self.assertEqual(probe["asked"], [sha("gone")],
                         "a failed read is retried on every repaint: %r" % (probe,))


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class OneRepaintMustNotCostTheRestOfTheThread(unittest.TestCase):
    def test_a_replaced_bubble_does_not_abandon_the_attachments_behind_it(self):
        """This is the "no media on the old messages" report. Hydration RETURNED on the first
        element that had left the document, and a repaint of the same conversation does exactly
        that — including the cold-load drain, which repaints after every batch. The longer the
        archive took to load, the fewer pictures ever appeared."""
        shas = [sha("photo %d" % n) for n in range(6)]
        uploads = {s: {"folder": "MMS", "name": "p.jpg", "type": "image/jpeg", "text": "photo"}
                   for s in shas}
        res = run(isPhone=False, cached=[], relayEmpty=True, uploads=uploads,
                  hydrateProbe={"parts": shas, "repaint": 1})
        done = res["hydrateProbe"]["done"]
        self.assertEqual(done[1], 0, "the replaced bubble was drawn into anyway")
        self.assertEqual([done[i] for i in (0, 2, 3, 4, 5)], [1, 1, 1, 1, 1],
                         "one repainted bubble abandoned the rest of the thread: %r" % (done,))

    def test_a_second_pass_leaves_already_drawn_attachments_alone(self):
        """paint() rebuilds #feed and hydration runs again over the same conversation. Work that
        is already on the screen must cost nothing — not merely nothing on the network (the
        address cache covers that), but no second draw either."""
        shas = [sha("photo %d" % n) for n in range(3)]
        uploads = {s: {"folder": "MMS", "name": "p.jpg", "type": "image/jpeg", "text": "photo"}
                   for s in shas}
        res = run(isPhone=False, cached=[], relayEmpty=True, uploads=uploads,
                  hydrateProbe={"parts": shas, "repaint": -1, "twice": True})
        probe = res["hydrateProbe"]
        self.assertEqual(probe["done"], [1, 1, 1])
        self.assertEqual(probe["drawn"], [1, 1, 1],
                         "a redraw re-drew every attachment already on screen: %r" % (probe,))
        self.assertEqual([c[1] for c in calls_of(res, "encFileUrl")], shas,
                         "a redraw re-read every attachment in the conversation")

    def test_leaving_the_screen_stops_the_reads_rather_than_the_writes(self):
        """Every placeholder still queued is disconnected the moment #feed is rewritten. Asked
        only AFTER the read, this pass went on fetching and decrypting the whole rest of the
        conversation — full-size encrypted media — for a screen nobody was looking at."""
        shas = [sha("photo %d" % n) for n in range(6)]
        uploads = {s: {"folder": "MMS", "name": "p.jpg", "type": "image/jpeg", "text": "photo"}
                   for s in shas}
        res = run(isPhone=False, cached=[], relayEmpty=True, uploads=uploads,
                  hydrateProbe={"parts": shas, "repaint": -1, "dead": [2, 3, 4, 5]})
        self.assertEqual(res["hydrateProbe"]["done"], [1, 1, 0, 0, 0, 0])
        self.assertEqual(sorted(c[1] for c in calls_of(res, "encFileUrl")), sorted(shas[:2]),
                         "attachments were read for bubbles that had already left the document")


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class APictureMessageWithNoPicture(unittest.TestCase):
    """MEASURED on the reporting account: 1,284 of 1,964 archived messages are flagged `mms:true`
    and carry no attachment at all — the handset published them before it could put the bytes in
    encrypted storage. Every one rendered as an ordinary bubble, an EMPTY one whenever the photo
    had no caption, so the conversation looked complete while the thing it was about was missing
    and nothing anywhere said a photo had ever been there.

    This does not repair the backup. It is the difference between a gap somebody can see and a gap
    nobody can — and it is what makes the phone-side fix verifiable from the screen.
    """

    def _run(self):
        rows = [dict(ev("pcai:sms:plain", body(1, body="just words"))["tags"] and {}) ]  # noqa
        archive = [
            ev("pcai:sms:plain", body(1, body="just words")),
            ev("pcai:sms:photo-no-caption", body(2, body="", mms=True)),
            ev("pcai:sms:photo-caption", body(3, body="look at this", mms=True)),
        ]
        return run(isPhone=False, cached=archive, relayEmpty=True, realArchiveFilters=True,
                   steps=["load", "settle"])

    def test_a_captionless_picture_message_is_not_an_empty_bubble(self):
        res = self._run()
        flat = [s for row in res["snippets"] for s in row]
        self.assertIn("Photo · not backed up", flat,
                      "a picture message with no media still renders as an empty bubble: %r" % (flat,))

    def test_a_caption_still_wins_over_the_notice(self):
        """The words somebody typed are the message. The notice is for the bubble that has none."""
        flat = [s for row in self._run()["snippets"] for s in row]
        self.assertIn("look at this", flat)
        self.assertIn("just words", flat)

    def test_the_count_is_on_the_screen_that_knows_it(self):
        line = self._run()["countLine"]
        self.assertIn("2 picture messages with no media backed up", line,
                      "the size of the gap is invisible: %r" % (line,))

    def test_an_archive_with_nothing_missing_reads_exactly_as_before(self):
        res = run(isPhone=False, cached=[ev("pcai:sms:a", body(1, body="hi"))], relayEmpty=True,
                  realArchiveFilters=True, steps=["load", "settle"])
        self.assertNotIn("no media backed up", res["countLine"])
