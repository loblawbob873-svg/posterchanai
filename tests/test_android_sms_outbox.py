"""SENDING A TEXT THIS PHONE WAS ASKED FOR, WITHOUT THE APP BEING OPEN.

Another device cannot reach a radio, so it writes an encrypted request at `pcai:smsout:<id>` and the
handset performs it. That half worked, with a limit that made it close to useless: the drain lived
in the client's JavaScript, which runs on load and on `visibilitychange` -- so the phone only acted
when somebody OPENED PosterChan on it. Reported as "it should not have to be visible".

THE HAZARD THIS FILE EXISTS FOR IS THE DOUBLE SEND. A sent text cannot be unsent. The JS drain sends
and THEN marks the request done, so a second reader looking a moment earlier sends it again. The two
are kept apart by the one fact that is exactly knowable: they are two halves of one process, and the
JS drain only runs while the app is visible. `AppVisible` is written by the Activity's lifecycle.

Each check here was verified to fail with its rule removed.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

SMS = os.path.join(ac.JAVA, "place", "poster", "app", "sms")
JAVAC = shutil.which("javac")
JAVARUN = shutil.which("java")

HARNESS = r"""
package place.poster.app.sms;

public class VisibleHarness {
  public static void main(String[] a) {
    // Default: nothing has started an Activity, so the client's drain is certainly not running and
    // the background one is the only sender.
    System.out.println("default\t" + AppVisible.is());
    AppVisible.set(true);
    System.out.println("resumed\t" + AppVisible.is());
    AppVisible.set(false);
    System.out.println("paused\t" + AppVisible.is());
  }
}
"""


@unittest.skipIf(not JAVAC or not JAVARUN, "no JDK on this node")
@unittest.skipIf(not os.path.isdir(SMS), "no android sources here")
class TheTwoDrainsNeverRunTogether(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = cls._run()

    @staticmethod
    def _run():
        tmp = tempfile.mkdtemp(prefix="pc-outbox-")
        h = os.path.join(tmp, "VisibleHarness.java")
        with open(h, "w") as f:
            f.write(HARNESS)
        r = subprocess.run([JAVAC, "-nowarn", "-d", tmp,
                            os.path.join(SMS, "AppVisible.java"), h],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-3000:]
        r = subprocess.run([JAVARUN, "-cp", tmp, "place.poster.app.sms.VisibleHarness"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-3000:]
        shutil.rmtree(tmp, ignore_errors=True)
        return dict(line.split("\t", 1) for line in r.stdout.splitlines() if "\t" in line)

    def test_it_defaults_to_not_visible(self):
        """The safe direction: a process with no Activity is one where the client's drain is
        certainly not running, so the background drain must be free to send."""
        self.assertEqual(self.out["default"], "false")

    def test_the_lifecycle_moves_it_both_ways(self):
        self.assertEqual(self.out["resumed"], "true")
        self.assertEqual(self.out["paused"], "false")


def _strip_comments(src):
    """Code only. Every rule below is also DESCRIBED in a comment beside it."""
    out, i = [], 0
    while i < len(src):
        if src.startswith("/*", i):
            j = src.find("*/", i + 2); i = len(src) if j < 0 else j + 2
        elif src.startswith("//", i):
            j = src.find("\n", i); i = len(src) if j < 0 else j
        else:
            out.append(src[i]); i += 1
    return "".join(out)


@unittest.skipIf(not os.path.isdir(SMS), "no android sources here")
class TheDrainRefusesWhatItMustRefuse(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _strip_comments(open(os.path.join(SMS, "SmsOutbox.java")).read())
        cls.act = _strip_comments(
            open(os.path.join(ac.JAVA, "place", "poster", "app", "MainActivity.java")).read())

    def test_it_refuses_while_the_app_is_on_screen(self):
        """THE DOUBLE SEND. Without this the native drain and the client's own both act on one
        request, and somebody's message goes out twice with no way to take it back."""
        self.assertIn("if (AppVisible.is()) return null;", self.src)

    def test_the_activity_reports_its_own_visibility(self):
        """A guard nothing writes to is a guard that is always false."""
        self.assertIn("AppVisible.set(true)", self.act)
        self.assertIn("AppVisible.set(false)", self.act)

    def test_a_send_is_marked_even_when_it_failed(self):
        """A text that went out and whose marker did not is a text that goes out AGAIN, and that is
        the one mistake with no undo. A failure is recorded rather than retried blindly."""
        i = self.src.index("SmsSender.Result r = SmsSender.send")
        after = self.src[i:i + 400]
        self.assertIn("return marker(", after)
        self.assertNotIn("if (r != null && r.ok) return marker", after)

    def test_a_stale_request_is_dropped_not_performed(self):
        """A text arriving a day late is worse than one that never went."""
        self.assertIn("MAX_AGE_MS", self.src)
        self.assertIn('"too old"', self.src)

    def test_an_already_done_request_is_left_alone(self):
        self.assertIn('req.optBoolean("done", false)', self.src)

    def test_it_is_sealed_to_the_users_own_key(self):
        """The request is NIP-44 to the account's own key -- the same thing the client wrote it
        with. Asking any other peer would decrypt nothing."""
        self.assertIn("Crypt.conversationKey(sec, me)", self.src)

    def test_incoming_sms_is_archived_without_the_webview(self):
        receiver = open(os.path.join(SMS, "SmsDeliverReceiver.java")).read()
        service = open(os.path.join(ac.JAVA, "place", "poster", "app", "signer",
                                    "SignerRelayService.java")).read()
        self.assertIn("SignerRelayService.archiveIncoming", receiver)
        self.assertIn("SmsOutbox.archiveIncoming", service)
        self.assertIn("publishSmsArchive", service)


if __name__ == "__main__":
    unittest.main()
