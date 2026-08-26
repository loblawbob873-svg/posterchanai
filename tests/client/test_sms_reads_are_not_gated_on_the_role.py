"""Reading and sending need permissions. The ROLE decides delivery. They are not the same question.

    "i just want sms to work like every other SMS app and to sync to nostr"

This one mistake has now been made in four separate places, found one at a time over a day, each
time producing a different-looking symptom:

  * `mirror`      gated on the role → a phone that had granted READ_SMS published NOTHING, ever.
  * `send`        gated on the role → a text typed on the handset was queued as a request for
                                      "your phone" to perform, on the phone holding it.
  * `importAll`   gated on the role → history was NEVER imported, so everything older than the day
                                      PosterChan was installed was missing, while new messages kept
                                      arriving and the archive looked healthy: "i still can't see
                                      texts I wrote in the past".
  * `drainOutbox` gated on the role → a laptop's send request sat unperformed on a handset that
                                      could perfectly well send it.

So the rule is asserted directly rather than trusted: no read and no send may consult `isPhone()`.
What the role legitimately gates is WRITING the phone's own message store — deleting a row, storing
a sent copy — and `remove` still does, correctly.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SMS = ROOT / "static/js/client/sms.js"


def fn(src, decl):
    i = src.index(decl)
    j = src.index("{", i)
    depth, k = 0, j
    while True:
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1


def strip_comments(src):
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("//", i):
            i = src.find("\n", i)
            if i < 0:
                break
        elif src.startswith("/*", i):
            i = src.find("*/", i)
            i = n if i < 0 else i + 2
        else:
            out.append(src[i]); i += 1
    return "".join(out)


class NoReadOrSendConsultsTheRole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = strip_comments(SMS.read_text())

    def test_publishing_the_archive(self):
        self.assertNotIn("isPhone()", fn(self.src, "async function mirror"))

    def test_importing_the_history(self):
        """The one that hid the longest: new messages still arrived, so the archive grew and looked
        healthy while everything older than the install date was never fetched."""
        body = fn(self.src, "async function importAll")
        self.assertNotIn("isPhone()", body)
        self.assertIn("canRead", body)

    def test_sending(self):
        self.assertNotIn("if(await isPhone()){", fn(self.src, "async function send(to, body, file)"))

    def test_performing_another_device_s_send(self):
        body = fn(self.src, "async function drainOutboxOnce")
        self.assertNotIn("isPhone()", body)
        self.assertIn("telephony", body)

    def test_a_device_with_no_radio_still_performs_nothing(self):
        """The rule that actually matters in drainOutbox: it is what stops a laptop and a phone both
        answering the same request."""
        self.assertIn("if(!stD.telephony) return 0;", fn(self.src, "async function drainOutboxOnce"))


class WritingTheProviderStillNeedsIt(unittest.TestCase):
    """The role is not meaningless — it decides what may be WRITTEN to the phone's own store."""

    @classmethod
    def setUpClass(cls):
        cls.src = strip_comments(SMS.read_text())

    def test_deleting_a_message_from_the_phone(self):
        self.assertIn("isPhone()", fn(self.src, "async function remove"))


if __name__ == "__main__":
    unittest.main()
