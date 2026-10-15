"""Sharing into Texts asks "send to whom?" with your conversations, not a blank phone-number box.

"android: should be able to share stuff from other apps with posterchan texts" (2026-10-15). The Texts share target
existed, but a share (no number in it) opened a dialog demanding a typed phone number. Now SendToActivity lists your
recent conversations ("New number…" first) and opens the chosen one exactly as the conversation list does.
"""
import os
import shutil
import subprocess
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMS = os.path.join(ROOT, "mobile/android/app/src/main/java/place/poster/app/sms")
JAVAC, JAVA_BIN = shutil.which("javac"), shutil.which("java")

DRIVE = r"""
package place.poster.app.sms;
public class Drive { public static void main(String[] a) {
  System.out.println("name=" + SendToPick.label("Alice", "+15551234567"));
  System.out.println("number=" + SendToPick.label("+15551234567", "+15551234567"));
  System.out.println("blank=" + SendToPick.label("", "+15551234567"));
  System.out.println("group=" + SendToPick.label(null, "+15551234567;+15557654321"));
  System.out.println("nothing=" + SendToPick.label(null, null));
}}
"""


@unittest.skipIf(not (JAVAC and JAVA_BIN), "no JDK on this node")
class Label(unittest.TestCase):
    def test_a_conversation_reads_as_its_contact_or_its_numbers(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "Drive.java"), "w").write(DRIVE)
            r = subprocess.run([JAVAC, "-nowarn", "-d", d, os.path.join(SMS, "SendToPick.java"), os.path.join(d, "Drive.java")],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            out = subprocess.run([JAVA_BIN, "-cp", d, "place.poster.app.sms.Drive"], capture_output=True, text=True, timeout=60)
        got = dict(l.split("=", 1) for l in out.stdout.splitlines())
        self.assertEqual(got, {"name": "Alice", "number": "+15551234567", "blank": "+15551234567",
                               "group": "+15551234567, +15557654321", "nothing": "?"})


class Wiring(unittest.TestCase):
    def test_a_share_without_a_number_picks_a_conversation(self):
        src = open(os.path.join(SMS, "SendToActivity.java")).read()
        on_create = src[src.index("protected void onCreate("):src.index("private void pickConversation(")]
        self.assertIn("Intent.ACTION_SEND.equals(in.getAction())", on_create)
        self.assertIn("pickConversation(in, body)", on_create)
        pick = src[src.index("private void pickConversation("):src.index("private void askNumber(")]
        self.assertIn("Messages.threads(app, 30, true)", pick, "the list must be the same one Texts shows")
        self.assertIn('new Thread(', pick, "the provider query must not run on the main thread")
        opened = src[src.index("private void openExisting("):]
        for extra in ("EXTRA_THREAD, t.id", "EXTRA_THREADS, t.ids", "EXTRA_ADDRESS, t.address", "SmsShare.forward(in, open)"):
            self.assertIn(extra, opened)

# A full compile needs Capacitor/OkHttp jars this node does not have; the CI APK build is that compile.
