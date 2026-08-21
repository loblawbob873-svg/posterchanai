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
            return json.loads(p["content"][4:]) if p["content"].startswith("enc:") else None
    return None


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class ThreadsHoldBothKinds(unittest.TestCase):

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
