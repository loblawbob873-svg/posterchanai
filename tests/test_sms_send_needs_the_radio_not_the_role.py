"""Sending a text needs SEND_SMS. Writing the phone's own message store needs the role.

    "POsterchan is not the this phones messaging app when i send message"

Two separate refusals produced that, and both were the same mistake in different places.

In `SmsSender.send`, a device without the ROLE was refused outright. The reasoning was recorded and
was sound as far as it went: a non-default app may call SmsManager but may not write the provider, so
the message would send and then be missing from the thread it was sent in — "it didn't send". It does
not hold on THIS screen, because the screen renders our own encrypted archive rather than the
provider. The copy that would be missing is the one in the phone's STOCK messages app. So the trade
was: refuse to send at all, on every phone that has not granted the role, to avoid a gap in a
different app's UI.

In the client, `send()` asked `isPhone()` — "do we hold the role" — and on a no it published the
message as a REQUEST for "your phone" to perform. On the phone itself. Nothing was ever going to pick
that up, so a text typed on the handset sat in a queue addressed to itself.

`stored` is what keeps this honest: false means the radio was asked and the phone's own store has no
copy, so the caller keeps its own and says so, rather than inventing a message nobody received.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENDER = ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/SmsSender.java"
PLUGIN = ROOT / "mobile/android/app/src/main/java/place/poster/app/sms/SmsPlugin.java"
SMS_JS = ROOT / "static/js/client/sms.js"


def block(src, decl, end="\n    }"):
    i = src.index(decl)
    return src[i:src.index(end, i)]


class TheRadioSendsWhetherOrNotWeHoldTheRole(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SENDER.read_text()

    def test_it_no_longer_refuses_without_the_role(self):
        self.assertNotIn('r.error = "PosterChan is not this phone\'s messages app"', self.src,
                         "a texting app that cannot text on a phone that has not granted the role")

    def test_the_role_now_only_gates_the_provider_write(self):
        self.assertIn("boolean mayWrite = HasRole.sms(ctx);", self.src)
        i = self.src.index("mayWrite")
        seg = self.src[i:i + 400]
        self.assertIn("storeSent", seg, "the role should gate the store write, nothing else")

    def test_it_still_sends(self):
        self.assertIn("sendMultipartTextMessage", self.src)

    def test_the_caller_is_told_whether_the_phone_kept_a_copy(self):
        self.assertIn("public boolean stored;", self.src)
        self.assertIn("r.stored = r.row != null;", self.src)

    def test_a_missing_row_is_not_marked_failed(self):
        """Without the role there is no row. Marking a null one failed is how a send that went out
        gets reported as broken."""
        self.assertIn("if (r.row != null) SmsStore.setType", self.src)

    def test_the_plugin_passes_it_across(self):
        self.assertIn('o.put("stored", r.stored);', PLUGIN.read_text())


class TheClientSendsFromTheDeviceHoldingTheRadio(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SMS_JS.read_text()
        cls.send = block(cls.src, "async function send(to, body)", "\n  }")

    def test_it_no_longer_gates_on_the_role(self):
        self.assertNotIn("if(await isPhone()){", self.send,
                         "a phone without the role queues its own text for itself to perform")

    def test_it_gates_on_the_device_having_a_radio(self):
        """`present` means the plugin answered — true of a tablet with no radio. `telephony` is
        whether this device can put a message on a network. The names are close enough to pick the
        wrong one, and I did: it sent a laptop's text down the radio path."""
        self.assertIn("st0.telephony", self.send)
        self.assertNotIn("if(st0.present){", self.send)

    def test_an_unstored_send_keeps_its_own_copy(self):
        """`mirror` republishes from the phone's store, and without the role there is no row there to
        find — so the message would send and then be missing from its own thread, which is the exact
        failure the old refusal existed to avoid."""
        self.assertIn("r.stored === false", self.send)
        self.assertIn("S.msgs.set(", self.send)
        self.assertIn("rebuild()", self.send)

    def test_a_stored_send_still_comes_from_the_provider(self):
        """The phone's row IS the message when there is one; inventing a second document for it
        would duplicate the thread."""
        i = self.send.index("r.stored === false")
        self.assertIn("mirror(", self.send[i:], "the stored path stopped republishing from the store")

    def test_nothing_is_invented_for_a_send_that_failed(self):
        """The one thing that must never enter the archive is a message nobody received."""
        i = self.send.index("S.msgs.set(")
        before = self.send[:i]
        self.assertIn("r && r.ok", before,
                      "the local copy is written outside the success branch")


if __name__ == "__main__":
    unittest.main()
