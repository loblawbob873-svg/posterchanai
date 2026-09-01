"""A REFUSED ATTACHMENT MUST NOT BE REFUSED FOR EVER.

Reported as: "Texts on webui/desktop still showing Photo · Photo · the phone answered with no bytes
and no reason despite it working before for that photo."

Those words are the PHONE's. When `archivePart` cannot read a part, the archive publishes the
message with the reason recorded on the attachment (`att.err`) and no hash, marks the row done and
lets the high-water mark move — deliberately, because the alternative was ten permanent refusals at
the old end of the store standing in front of everything newer, with `published: 0` sweep after
sweep. Every other device then renders the phone's sentence, which is truthful and, on a desktop,
completely dead-ended: the picture is fine on the handset and the archive simply never got bytes.

`needsPartUpgrade`'s `settled` is what made it permanent — a part with an `err` was never offered
again unless somebody pressed Rescan, and Rescan is on the phone while the failure is only visible
on the desktop. Nothing connected the two.

The cost that motivated `settled` is real: re-offering every refusal on every sweep is one relay
write per refused picture per pass, 1,284 of them on the reporting account. But the comment right
above it says the provider "is allowed to refuse the second time even though it answered the first"
— these refusals are TRANSIENT. A budget bounds the cost without giving up: each sweep re-offers a
few refused documents, so a transient refusal heals by itself over a handful of visits and a
permanent one is retried cheaply. Rescan still means "all of them, now", because a person asking is
a different thing from a timer asking.

Each check here was verified to fail with the rule removed.

Run: venv-unified/bin/python -m pytest tests/client/test_sms_refused_attachment_heals.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "sms_sim.js")
SMS_JS = os.path.join(ROOT, "static", "js", "client", "sms.js")
NODE = shutil.which("node")
NOW = int(time.time() * 1000)


def ev(d, payload, at=1000):
    """One archived document, the shape the relay actually holds."""
    return {"kind": 30078, "content": "enc:" + json.dumps(payload), "created_at": at,
            "pubkey": "me", "id": "x" + d, "tags": [["d", d], ["l", "pcai-sms"]]}


def blob_ev(d, payload, at=1000):
    """An archived document in the shape the production relay actually holds — a Blossom pointer,
    not an inline body. It matters here: `_blob` is what marks a document COMPLETE, and a fixture
    that inlines the body would be re-published for that reason alone, hiding whether the
    never-asked rule did anything."""
    text = json.dumps(payload)
    digest = hashlib.sha256(text.encode()).hexdigest()
    upload = {digest: {"folder": "Messages", "name": "m.json", "type": "application/json",
                       "text": text}}
    return (ev(d, {"v": 1, "blob": digest, "mime": "application/json"}, at), upload)


def picture(i, *, refuse=True):
    """One MMS row whose single part the provider will not hand over."""
    return {"id": i, "thread": 1, "address": "+15550100", "body": "", "date": NOW - 60000 + i * 10,
            "type": 1, "incoming": True, "read": True, "mms": True,
            "parts": [{"id": 900 + i, "ct": "image/jpeg", "name": "p%d.jpg" % i, "bytes": 2048}],
            "doc": "pcai:sms:%024d" % i}


def parts_for(rows, *, refuse):
    out = {}
    for r in rows:
        for p in r["parts"]:
            # The bytes are always present: a refusal here is the PROVIDER declining a read, not
            # a file that does not exist, which is exactly why re-reading it later can succeed.
            out[str(p["id"])] = {"data": "eA==", "total": 1}
            if refuse:
                out[str(p["id"])].update({"refuse": "provider refused attachment", "total": 0})
    return out


def run(**opts):
    done = subprocess.run([NODE, SIM, json.dumps(opts)], capture_output=True, timeout=180)
    if done.returncode != 0:
        raise AssertionError(done.stderr.decode()[-4000:])
    return json.loads(done.stdout.decode())


def shas(res):
    """Every attachment hash the archive ended up holding."""
    return [s for t in res["threads"] for row in t["partShas"] for s in row if s]


@unittest.skipIf(NODE is None, "node not installed")
class ARefusalHeals(unittest.TestCase):
    def test_a_refused_attachment_is_archived_with_its_reason_and_no_hash(self):
        """The starting state, so the healing tests below cannot pass vacuously. This half is the
        existing, deliberate behaviour: the row is DONE and the mark moves."""
        rows = [picture(1)]
        res = run(isPhone=True, rows=rows, parts=parts_for(rows, refuse=True),
                  steps=["phoneLoad", "migrate", "settle"])
        self.assertEqual(shas(res), [], "a refused part was archived with a hash anyway")
        self.assertTrue(any(a.get("refused") for a in res["archive"]),
                        "the refusal was not recorded on the document: %r" % (res["archive"],))

    def test_the_next_sweep_picks_it_up_once_the_provider_answers(self):
        """THE REPORT. Nothing is pressed: the provider simply cooperates on a later read, which is
        what "it worked before for that photo" means."""
        rows = [picture(1)]
        res = run(isPhone=True, rows=rows, parts=parts_for(rows, refuse=True),
                  steps=["phoneLoad", "migrate", "settle",
                         "allowParts", "mirror", "settle"])
        self.assertTrue(shas(res),
                        "the picture was never re-offered, so the archive still has no bytes for "
                        "it and every other device shows the phone's old sentence for ever")

    def test_it_still_heals_without_anyone_opening_the_phones_texts_screen(self):
        """The distinction that matters: this must not require `rescan`, because Rescan is a button
        on the handset and the person looking at the gap is on a laptop."""
        rows = [picture(1)]
        res = run(isPhone=True, rows=rows, parts=parts_for(rows, refuse=True),
                  steps=["phoneLoad", "migrate", "settle", "allowParts", "mirror", "settle"])
        self.assertNotIn("rescan", [c[0] for c in res["calls"]],
                         "the fixture pressed Rescan — this test proves nothing")
        self.assertTrue(shas(res))

    def test_a_refusal_that_stays_a_refusal_does_not_grow_a_hash(self):
        """The obvious wrong fix is to stop recording refusals. A part the provider still will not
        hand over must remain honestly empty."""
        rows = [picture(1)]
        res = run(isPhone=True, rows=rows, parts=parts_for(rows, refuse=True),
                  steps=["phoneLoad", "migrate", "settle", "mirror", "settle"])
        self.assertEqual(shas(res), [])


@unittest.skipIf(NODE is None, "node not installed")
class TheRetryIsBounded(unittest.TestCase):
    """The reason `settled` existed. 1,284 refused pictures re-offered on every sweep is 1,284
    relay writes per pass on a phone that is also trying to paint a screen."""

    def test_one_sweep_does_not_re_offer_every_refusal(self):
        rows = [picture(i) for i in range(1, 41)]
        res = run(isPhone=True, rows=rows, parts=parts_for(rows, refuse=True),
                  steps=["phoneLoad", "migrate", "settle", "mirror", "settle"])
        reads = [c for c in res["calls"] if c[0] == "attachment"]
        self.assertLess(len(reads), 40 * 2, "the sweep re-read every refused attachment: %d" % len(reads))

    def test_the_budget_is_a_number_in_the_source_not_a_side_effect(self):
        """If this becomes unbounded again the failure is a phone that stops responding, which is
        the shape the original 1,284-write report took."""
        src = open(SMS_JS, encoding="utf-8").read()
        self.assertIn("REFUSED_RETRY_PER_SWEEP", src)
        self.assertIn("_resetRefusedRetryBudget()", src)

    def test_a_person_pressing_rescan_still_means_all_of_them(self):
        """`rescan` is the deliberate, unbounded version and must not have been capped by the
        budget — a person asking is a different thing from a timer asking."""
        rows = [picture(i) for i in range(1, 21)]
        res = run(isPhone=True, rows=rows, parts=parts_for(rows, refuse=True),
                  steps=["phoneLoad", "migrate", "settle", "allowParts", "rescan", "settle"])
        self.assertGreaterEqual(len(shas(res)), 20,
                                "Rescan no longer retries every refused attachment: %d" % len(shas(res)))


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(NODE is None, "node not installed")
class TheLabelIsSaidOnce(unittest.TestCase):
    """Reported verbatim: "Photo · Photo · the phone answered with no bytes and no reason".

    `partData` builds its message as `attLabel(p) + ' · ' + reason`, and `archivePart` throws that
    whole string — so publishOne stored "Photo · provider refused attachment" as the REASON, and
    every reader then put the label in front of it again. The simulator had been printing the
    doubled form all along (`"err":"Photo · provider refused attachment"`).

    It compounds per archive rather than per read, so it is stored, not cosmetic: those documents
    are published Nostr events and there is no migration for them. Hence two rules — store the bare
    reason from now on, and strip a leading label when displaying, so the archive somebody already
    has reads correctly too."""

    def _archived_err(self, res):
        return [e for t in res["threads"] for row in t["partErrs"] for e in row if e]

    def test_a_new_refusal_is_stored_without_the_label(self):
        rows = [picture(1)]
        res = run(isPhone=True, rows=rows, parts=parts_for(rows, refuse=True),
                  steps=["phoneLoad", "migrate", "settle"])
        errs = self._archived_err(res)
        self.assertTrue(errs, "no refusal was recorded at all")
        for e in errs:
            self.assertFalse(e.startswith("Photo"), f"the label is baked into the stored reason: {e!r}")
            self.assertIn("refused", e)

    def test_the_label_is_not_repeated_on_screen(self):
        """What the person actually sees. The bubble text must name the attachment once."""
        rows = [picture(1)]
        res = run(isPhone=True, rows=rows, parts=parts_for(rows, refuse=True),
                  steps=["phoneLoad", "migrate", "settle", "render"])
        shown = " ".join(s for row in res["snippets"] for s in row) + " " + res.get("feedHtml", "")
        self.assertNotIn("Photo · Photo", shown, "the label is still doubled on screen")

    def test_an_archive_that_already_has_the_doubled_form_reads_correctly(self):
        """The half that cannot be fixed by writing better documents: these are published events,
        and every one archived before this reads "Photo · Photo · …" for ever without it."""
        src = open(SMS_JS, encoding="utf-8").read()
        self.assertIn("function _bareReason(", src)
        self.assertIn("function attReason(", src)
        # The display path must go through it rather than concatenating the label itself.
        self.assertNotIn("{ why: attLabel(p) + ' \\u00b7 '\n      + (String(p.err", src)


def bare_mms(i):
    """A picture message the way the phone's TIMELINE query returns it: flagged mms, no parts.
    `withMmsParts` is what fills those in, and it only runs for rows already in the batch."""
    return {"id": i, "thread": 1, "address": "+15550100", "body": "", "date": NOW - 60000 + i * 10,
            "type": 1, "incoming": True, "read": True, "mms": True,
            "doc": "pcai:sms:%024d" % i}


@unittest.skipIf(NODE is None, "node not installed")
class APictureMessageNobodyAsked(unittest.TestCase):
    """MEASURED ON THE REPORTING ACCOUNT, over CDP, while it was saying "Texts still not showing my
    images on desktop/webui":

        messages 2260 · mms 1490
        mms with NO attachment at all : 1299
        parts total                   :  202  (182 with a hash, 20 refused)

    So the visible error — "the phone answered with no bytes and no reason" — was TWENTY messages.
    The 1,299 were the whole of the report, and they could not repair themselves:

      * `needsPartUpgrade` reads a row's parts to decide, and a bare timeline row has none, so it
        answered "complete";
      * that kept the row out of the batch, and `withMmsParts` only fills rows IN the batch.

    It needed parts to get in, and it needed to get in to get parts. `natt` was already in the wire
    format and in publishOne's memo for exactly this — but nothing ever WROTE it and nothing ever
    READ it to decide, so "never asked" and "asked, nothing there" were the same state.
    """

    def test_a_bare_picture_message_is_offered_its_attachments(self):
        """THE REPORT, in the shape the sim already models as the reporting account's: the combined
        timeline hands back the picture message with NO parts, while the direct MMS walk has the
        same row complete. That asymmetry is why a handset shows its own pictures and every other
        device gets text."""
        parts = {"901": {"data": "eA==", "total": 1}}
        row = dict(bare_mms(1),
                   parts=[{"id": 901, "ct": "image/jpeg", "name": "p.jpg", "bytes": 2048}])
        res = run(isPhone=True, rows=[row], parts=parts, combinedDropsParts=True,
                  steps=["phoneLoad", "migrate", "settle"])
        self.assertTrue(shas(res),
                        "a picture message with no attachment record was never offered its parts, "
                        "so it stays a bubble with no picture for ever: %r" % (res["archive"],))

    def test_the_archive_records_that_it_asked(self):
        """Without an ANSWER on the document, the next sweep re-derives "never asked" from the same
        absence and republishes it — one relay write per picture per pass, which is the loop the
        refusal marker was invented to end."""
        src = open(SMS_JS, encoding="utf-8").read()
        self.assertIn("else if(m.mms) body.natt = 1;", src,
                      "publishOne still never writes natt, so nothing can tell 'asked and there "
                      "was nothing' from 'nobody has asked'")
        # NOTE: a companion rule that re-offers a message ALREADY archived without media was tried
        # and REMOVED. Three fixtures were built for it and none failed when it was deleted — in
        # every reproducible case the existing logic already admits the row — so it was shipping an
        # unprovable widening of the migration filter over somebody's 2,260-message archive. If the
        # case is ever constructed, that is the moment to add it back.

    def test_an_answered_message_is_not_asked_again(self):
        """The settle half. A genuine text-only MMS must not be re-offered every sweep."""
        rows = [bare_mms(1)]
        res = run(isPhone=True, rows=rows, parts={}, combinedDropsParts=True,
                  steps=["phoneLoad", "migrate", "settle", "mirror", "settle"])
        doc = rows[0]["doc"]
        wrote = [p for p in res["published"] if p["d"] == doc]
        self.assertLessEqual(len(wrote), 2,
                             "an attachment-less picture message is republished on every sweep: "
                             "%d writes" % (len(wrote),))

    def test_a_refusal_and_an_empty_answer_are_recorded_differently(self):
        """`err` means the provider refused; `natt` means it was asked and had nothing. They are
        different facts about a picture message with no media, and collapsing them is what made
        "nobody has asked" indistinguishable from "asked, there was nothing there"."""
        src = open(SMS_JS, encoding="utf-8").read()
        self.assertIn("else if(m.mms) body.natt = 1;", src)
        self.assertIn("if(!sha && p.err) att.err = p.err;", src,
                      "a refusal is no longer recorded on the attachment")

    def test_a_bare_picture_message_is_ASKED_ABOUT_and_the_answer_recorded(self):
        """THE PROPERTY, and the whole of the 1,299.

        A picture message with no attachment was excluded from the migration queue, because
        `needsPartUpgrade` reads parts to decide and there were none. The queue therefore drained
        while 1,299 of 1,490 pictures had no media, the completion latch went down, and
        `migrateLocalHistory` returned immediately for ever after — the screen saying the archive
        had finished.

        Counted now, each one is ASKED. The answer is either the bytes or `natt` ("asked, the
        provider had nothing"), and a recorded answer is what stops it being asked again — the
        difference between draining a backlog and a treadmill."""
        rows = [bare_mms(1)]
        res = run(isPhone=True, rows=rows, parts={},
                  steps=["phoneLoad", "migrate", "settle"])
        docs = [a for a in res["archive"] if a["d"] == rows[0]["doc"]]
        self.assertTrue(docs, "the bare picture message was never published at all")
        self.assertTrue(any("natt" in (a.get("keys") or []) for a in docs),
                        "it was archived again without recording that the provider was asked, so "
                        "the next sweep re-derives 'never asked' from the same absence: %r"
                        % (docs[-1].get("keys"),))

    def test_a_picture_message_already_archived_without_media_is_asked_again(self):
        """THE 1,299, exactly: archived, COMPLETE by every existing measure (`_blob` set, no parts
        to be unhappy about, no refusal recorded), and therefore never offered again.

        `needsPartUpgrade` reads a row's parts to decide and there are none, so it answers
        "complete"; the row stays out of the queue; the queue drains; the completion latch goes
        down; `migrateLocalHistory` returns immediately for ever. Measured on the reporting account:
        1,490 picture messages, 182 with media, and the visible error accounted for only 20 of the
        rest.

        `natt` is what distinguishes "asked, the provider had nothing" from "nobody has asked". A
        legacy document carries neither, so it must be asked once — and then settled."""
        row = dict(bare_mms(1),
                   parts=[{"id": 901, "ct": "image/jpeg", "name": "p.jpg", "bytes": 2048}])
        e, up = blob_ev(row["doc"], {"address": row["address"], "body": "", "date": row["date"],
                                     "incoming": True, "mms": True})
        res = run(isPhone=True, rows=[row], parts={"901": {"data": "eA==", "total": 1}},
                  cached=[e], uploads=up,
                  steps=["load", "phoneLoad", "migrate", "settle"])
        self.assertTrue(shas(res),
                        "a picture message that was archived without its media is never offered "
                        "again, so the photo stays off every other device for ever: %r"
                        % (res["archive"][-1:] if res["archive"] else None,))
