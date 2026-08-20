"""THE DIALER — its rules RUN, and the components Android demands before the role can be granted.

Audio routing and call state are where a dialer goes wrong, and the failures are never exceptions:
they are a button that does nothing. The platform answers an impossible request — answering a call
that is already connected, holding one that is still ringing, sending DTMF down a call that has not
connected — by doing nothing at all. No throw, no callback, no log. So the legality of every control
lives in CallRules, which has no Android in it, and this file runs the table.

`Dial` is here for the same reason: a dialpad looks like the simplest screen in a phone and carries
three rules whose failure is invisible (`+` outside the first position, the pause characters, and a
`#` that a URI parser eats as a fragment).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

ROOT = ac.ROOT
PHONE = os.path.join(ac.JAVA, "place", "poster", "app", "phone")
MANIFEST = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "AndroidManifest.xml")
JAVAC = shutil.which("javac")
JAVARUN = shutil.which("java")

HARNESS = r"""
import place.poster.app.phone.CallRules;
import place.poster.app.phone.Dial;

public class TelHarness {
  static void say(String k, Object v) { System.out.println(k + "\t" + v); }
  static String row(int s) {
    return CallRules.canAnswer(s) + "," + CallRules.canReject(s) + "," + CallRules.canHangUp(s)
         + "," + CallRules.canHold(s) + "," + CallRules.canUnhold(s)
         + "," + CallRules.canSendTones(s) + "," + CallRules.canRoute(s);
  }
  public static void main(String[] a) {
    say("new",          row(CallRules.STATE_NEW));
    say("connecting",   row(CallRules.STATE_CONNECTING));
    say("dialing",      row(CallRules.STATE_DIALING));
    say("ringing",      row(CallRules.STATE_RINGING));
    say("active",       row(CallRules.STATE_ACTIVE));
    say("holding",      row(CallRules.STATE_HOLDING));
    say("disconnecting", row(CallRules.STATE_DISCONNECTING));
    say("disconnected", row(CallRules.STATE_DISCONNECTED));

    say("over", CallRules.isOver(CallRules.STATE_DISCONNECTED) + " " + CallRules.isOver(CallRules.STATE_ACTIVE));

    // Which call the screen shows when there are several.
    say("primary-ring", CallRules.primary(new int[]{ CallRules.STATE_HOLDING,
                                                     CallRules.STATE_RINGING,
                                                     CallRules.STATE_ACTIVE }));
    say("primary-active", CallRules.primary(new int[]{ CallRules.STATE_HOLDING,
                                                       CallRules.STATE_ACTIVE }));
    say("primary-none", CallRules.primary(new int[]{ CallRules.STATE_DISCONNECTED }));
    say("primary-empty", CallRules.primary(new int[0]) + " " + CallRules.primary(null));

    say("label-active", "[" + CallRules.label(CallRules.STATE_ACTIVE, false) + "]");
    say("label-ringing-in", CallRules.label(CallRules.STATE_RINGING, true));
    say("label-ringing-out", CallRules.label(CallRules.STATE_RINGING, false));

    // Dialpad.
    say("plus-first", Dial.press("", '+'));
    say("plus-later", Dial.press("555", '+'));
    say("junk", Dial.press("555", 'x'));
    say("pauses", Dial.clean("+1 (555) 010-4477,,1234"));
    say("letters", Dial.clean("call Alice 555"));
    say("service", Dial.isServiceCode("*#06#") + " " + Dial.isServiceCode("*21*15550100#")
                 + " " + Dial.isServiceCode("15550100"));
    say("dialable", Dial.dialable(",,,") + " " + Dial.dialable("") + " " + Dial.dialable("*#06#")
                  + " " + Dial.dialable("5550100"));
    say("back", Dial.backspace("555") + " [" + Dial.backspace("") + "]");
    say("pretty", Dial.pretty("5550104477") + " | " + Dial.pretty("+15550104477")
                + " | " + Dial.pretty("*#06#") + " | " + Dial.pretty("+1555010447712345"));
  }
}
"""


@unittest.skipIf(not JAVAC or not JAVARUN, "no JDK on this node")
@unittest.skipIf(not os.path.isdir(PHONE), "no android sources here")
class DialerRules(unittest.TestCase):
    out = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        h = os.path.join(cls.tmp, "TelHarness.java")
        with open(h, "w") as f:
            f.write(HARNESS)
        src = [os.path.join(PHONE, "CallRules.java"), os.path.join(PHONE, "Dial.java")]
        r = subprocess.run([JAVAC, "-nowarn", "-d", cls.tmp, "-sourcepath", ac.JAVA] + src + [h],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]
        r = subprocess.run([JAVARUN, "-cp", cls.tmp, "TelHarness"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-4000:]
        cls.out = dict(line.split("\t", 1) for line in r.stdout.splitlines() if "\t" in line)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # answer,reject,hangup,hold,unhold,tones,route
    def test_only_a_ringing_call_can_be_answered(self):
        self.assertTrue(self.out["ringing"].startswith("true,true,true"))
        for state in ("active", "holding", "dialing", "connecting", "disconnected"):
            self.assertTrue(self.out[state].startswith("false,false"), state)

    def test_a_call_that_has_ended_offers_nothing(self):
        self.assertEqual(self.out["disconnected"], "false,false,false,false,false,false,false")

    def test_hold_needs_a_connection_to_hold(self):
        self.assertEqual(self.out["active"].split(",")[3], "true")
        self.assertEqual(self.out["ringing"].split(",")[3], "false")
        self.assertEqual(self.out["dialing"].split(",")[3], "false")
        # And unhold is the other half — offered only while held.
        self.assertEqual(self.out["holding"].split(",")[4], "true")
        self.assertEqual(self.out["active"].split(",")[4], "false")

    def test_dtmf_only_reaches_a_connected_call(self):
        """A dialing call swallows every tone — the phone-tree digits somebody typed while it rang
        are simply lost, with the keypad drawing them the whole time."""
        self.assertEqual(self.out["active"].split(",")[5], "true")
        self.assertEqual(self.out["dialing"].split(",")[5], "false")
        self.assertEqual(self.out["ringing"].split(",")[5], "false")

    def test_the_speaker_works_from_the_moment_it_connects(self):
        """Somebody turning the speaker on while it rings expects it on when they are answered."""
        for state in ("connecting", "dialing", "ringing", "active", "holding"):
            self.assertEqual(self.out[state].split(",")[6], "true", state)
        self.assertEqual(self.out["disconnected"].split(",")[6], "false")

    def test_a_ringing_call_outranks_everything_on_screen(self):
        """A screen showing the held call while another one rings is a screen whose hang-up button
        ends the wrong call."""
        self.assertEqual(self.out["primary-ring"], "1")
        self.assertEqual(self.out["primary-active"], "1")
        self.assertEqual(self.out["primary-none"], "-1")
        self.assertEqual(self.out["primary-empty"], "-1 -1")

    def test_an_active_call_shows_a_timer_rather_than_a_word(self):
        self.assertEqual(self.out["label-active"], "[]")
        self.assertEqual(self.out["label-ringing-in"], "Incoming call")
        self.assertEqual(self.out["label-ringing-out"], "Ringing")

    def test_plus_is_only_a_plus_at_the_front(self):
        """Held on the zero key anywhere else it is part of a number nobody can call."""
        self.assertEqual(self.out["plus-first"], "+")
        self.assertEqual(self.out["plus-later"], "555")
        self.assertEqual(self.out["junk"], "555")

    def test_the_pause_characters_survive(self):
        """`+15550100,,1234` dials the extension after the call connects. Stripping the commas 'to
        clean up the number' quietly breaks every stored phone-tree shortcut somebody has."""
        self.assertEqual(self.out["pauses"], "+15550104477,,1234")

    def test_a_pasted_name_is_reduced_to_what_a_radio_can_dial(self):
        self.assertEqual(self.out["letters"], "555")

    def test_a_service_code_is_recognised(self):
        """It must go through ACTION_DIAL so the platform can intercept it. Placed as a call it
        either fails or silently changes a network setting."""
        self.assertEqual(self.out["service"], "true true false")

    def test_a_string_of_pauses_is_not_a_number(self):
        self.assertEqual(self.out["dialable"], "false false true true")

    def test_backspace_on_an_empty_pad_is_not_an_error(self):
        self.assertEqual(self.out["back"], "55 []")

    def test_the_prettifier_never_mangles_what_it_does_not_understand(self):
        """A number shown wrong is worse than a number shown plainly, so anything that is not an
        obvious shape comes back untouched."""
        pretty = self.out["pretty"].split(" | ")
        self.assertEqual(pretty[0], "555 010 4477")
        self.assertEqual(pretty[1], "+1 555 010 4477")
        self.assertEqual(pretty[2], "*#06#")
        self.assertEqual(pretty[3], "+1555010447712345")


@unittest.skipIf(ac.android_jar() is None, "no android.jar on this node")
@unittest.skipIf(not JAVAC or not JAVARUN, "no android sources here")
class CallStateNumbers(unittest.TestCase):
    """CallRules repeats android.telecom.Call.STATE_* as its own constants so the file stays free of
    the platform. That is a second copy of a value, which in this codebase is the shape that drifts —
    so it is checked against the real SDK rather than trusted."""

    def test_the_state_numbers_match_the_platforms(self):
        probe = r"""
import android.telecom.Call;
import place.poster.app.phone.CallRules;
public class StateProbe {
  static void eq(String n, int a, int b) {
    if (a != b) throw new AssertionError(n + ": ours " + a + " platform " + b);
    System.out.println(n + " ok");
  }
  public static void main(String[] x) {
    eq("NEW", CallRules.STATE_NEW, Call.STATE_NEW);
    eq("DIALING", CallRules.STATE_DIALING, Call.STATE_DIALING);
    eq("RINGING", CallRules.STATE_RINGING, Call.STATE_RINGING);
    eq("HOLDING", CallRules.STATE_HOLDING, Call.STATE_HOLDING);
    eq("ACTIVE", CallRules.STATE_ACTIVE, Call.STATE_ACTIVE);
    eq("DISCONNECTED", CallRules.STATE_DISCONNECTED, Call.STATE_DISCONNECTED);
    eq("CONNECTING", CallRules.STATE_CONNECTING, Call.STATE_CONNECTING);
    eq("DISCONNECTING", CallRules.STATE_DISCONNECTING, Call.STATE_DISCONNECTING);
    eq("SELECT_PHONE_ACCOUNT", CallRules.STATE_SELECT_PHONE_ACCOUNT, Call.STATE_SELECT_PHONE_ACCOUNT);
    eq("PULLING_CALL", CallRules.STATE_PULLING_CALL, Call.STATE_PULLING_CALL);
    eq("AUDIO_PROCESSING", CallRules.STATE_AUDIO_PROCESSING, Call.STATE_AUDIO_PROCESSING);
    eq("SIMULATED_RINGING", CallRules.STATE_SIMULATED_RINGING, Call.STATE_SIMULATED_RINGING);
  }
}
"""
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "StateProbe.java")
            with open(p, "w") as f:
                f.write(probe)
            r = ac.compile_sources([os.path.join(PHONE, "CallRules.java"), p], tmp)
            self.assertEqual(r.returncode, 0, r.stderr[-3000:])
            jar = ac.android_jar()
            run = subprocess.run([JAVARUN, "-cp", os.path.join(tmp, "classes") + os.pathsep + jar,
                                  "StateProbe"], capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


@unittest.skipIf(not os.path.isdir(PHONE), "no android sources here")
class DialerRole(unittest.TestCase):

    def setUp(self):
        self.man = open(MANIFEST, encoding="utf-8").read()
        # WITHOUT THE COMMENTS for the "must not be present" assertions. Every one of those rules is
        # explained in a comment naming the thing it forbids, so a raw substring search fails on the
        # documentation of the rule it is checking.
        self.code = re.sub(r"<!--.*?-->", " ", self.man, flags=re.S)

    def test_the_incall_service_declares_it_draws_the_ui(self):
        """Without IN_CALL_SERVICE_UI telecom treats this as an observer and keeps its own call
        screen; without RINGING it keeps its own ringer. Either way ours is never asked, and there is
        nothing to say so."""
        i = self.man.index(".phone.PcInCallService")
        block = self.man[i:i + 1000]
        self.assertIn("android.telecom.IN_CALL_SERVICE_UI", block)
        self.assertIn("android.telecom.IN_CALL_SERVICE_RINGING", block)
        self.assertIn("android.telecom.InCallService", block)
        self.assertIn("android.permission.BIND_INCALL_SERVICE", block)

    def test_the_dialer_answers_action_dial_with_and_without_a_number(self):
        """Both filters, because Android requires both before it will offer this app as the default
        phone app — and an app that is not offered has a switch that appears to do nothing."""
        i = self.man.index(".phone.DialerActivity")
        block = self.man[i:i + 2200]
        self.assertEqual(block.count("android.intent.action.DIAL"), 2)
        self.assertIn('android:scheme="tel"', block)

    def test_the_call_screen_shows_over_the_lock_screen(self):
        i = self.man.index(".phone.InCallActivity")
        block = self.man[i:i + 900]
        self.assertIn("showOnLockScreen", block)
        self.assertIn("turnScreenOn", block)

    def test_the_dialer_can_see_another_dialer(self):
        """A service code is handed to the platform's own dialer, and a call telecom refuses falls
        back to it. Android 11+ hides the package list, so without a <queries> entry both paths
        silently do nothing."""
        q = self.man[self.man.index("<queries>"):self.man.index("</queries>")]
        self.assertIn("android.intent.action.DIAL", q)

    def test_it_does_not_ask_for_a_permission_it_does_not_need(self):
        """An InCallService may answer a call it was handed without ANSWER_PHONE_CALLS. Asking for it
        anyway is how a phone app earns a reputation."""
        self.assertNotIn("ANSWER_PHONE_CALLS", self.code)

    def test_the_cellular_and_nostr_calls_share_nothing(self):
        """Both live in the same process. `place.poster.app.CALL_HANGUP` belongs to the Nostr WebRTC
        service and its onStartCommand dispatches on the action without checking who sent it — a
        shared verb would let a cellular hang-up tear down an internet call, or the reverse. The
        notification channels are separate for the person's sake: silencing one must not silence the
        other."""
        src = ""
        for f in os.listdir(PHONE):
            if not f.endswith(".java"):
                continue
            one = open(os.path.join(PHONE, f), encoding="utf-8").read()
            one = re.sub(r"/\*.*?\*/", " ", one, flags=re.S)
            src += re.sub(r"//[^\n]*", " ", one)
        for taken in ("place.poster.app.CALL_START", "place.poster.app.CALL_STOP",
                      "place.poster.app.CALL_HANGUP", "place.poster.app.CALL_UPDATE"):
            self.assertNotIn(taken, src, "the dialer reuses the Nostr call service's " + taken)
        for taken in ('"pcai_calls"', '"pcai_ongoing_calls"', '"pcai_messages"'):
            self.assertNotIn(taken, src, "the dialer reuses an existing notification channel")
        self.assertIn("pcai_cell_incoming", src)
        self.assertIn("pcai_cell_ongoing", src)

    def test_nothing_in_the_dialer_holds_a_wake_lock(self):
        """The call screen keeps the screen on with a WINDOW flag, which is scoped to the activity and
        released with it. A PowerManager lock survives whatever forgets to release it, and on a phone
        where this app also holds the HOME role that is a lock held for the life of the battery."""
        for f in os.listdir(PHONE):
            if not f.endswith(".java"):
                continue
            src = open(os.path.join(PHONE, f), encoding="utf-8").read()
            src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
            src = re.sub(r"//[^\n]*", " ", src)
            self.assertNotIn("newWakeLock", src, f)
            self.assertNotIn("PeriodicWorkRequest", src, f)
            self.assertNotIn("setRepeating", src, f)


if __name__ == "__main__":
    unittest.main()
