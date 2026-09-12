"""A PAUSE IS PART OF THE NUMBER, AND THE PHONE COULD NOT TYPE ONE OR FINISH ONE.

"Phone app does not support the pause in phone numbers!"

`+18005550100,,123#` is one phone number: dial the switchboard, wait about four seconds, then send
the extension. `,` is a two-second pause the radio counts out on its own; `;` is a WAIT, which stops
until the app says to send the rest. Every dialer on every phone has had both since before
smartphones, and this one had neither — in three separate places, each of which fails in silence:

  1. THERE WAS NO WAY TO TYPE ONE. `Dial.DIALABLE` has always accepted `,` and `;` and `clean` has
     always kept them, but the pad wired exactly one long press — `+` on the zero key
     (`Keypad.key`: `if ("0".equals(digit)) { ... press.onKey('+'); }`) — and the number field is a
     `TextView`, not an `EditText`, so there is no keyboard and no paste either. Support for a
     character with no way to enter it is indistinguishable from no support.

  2. `p` AND `w` WERE DROPPED. A number imported from another phone or a carrier's own card spells
     the same two things `p` and `w`. `clean` kept only `0123456789*#+,;N`, so `8005550100p1234`
     became `80055501001234` — not a failure, a DIFFERENT NUMBER, dialled at a stranger.

  3. A `;` NEVER CONTINUED. Telephony hands the remainder of the number back through
     `Call.Callback.onPostDialWait` and waits for `Call.postDialContinue`. `PcInCallService`'s
     callback overrode `onStateChanged`, `onDetailsChanged` and `onCallDestroyed` — and nothing
     else. So the rest was never sent, on any call, ever: no exception, no log, and a call on screen
     that looked completely normal and had simply stopped halfway through the number.

The rules are RUN here, not grepped for. `Dial` has no Android in it precisely so that this file can
javac it and execute the awkward cases, which is also why `Dial.held` — what a long press types —
lives there rather than in `Keypad`, which builds Views and can only ever be compiled.
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

PHONE = os.path.join(ac.JAVA, "place", "poster", "app", "phone")
JAVAC = shutil.which("javac")
JAVARUN = shutil.which("java")

HARNESS = r"""
import place.poster.app.phone.Dial;

public class PauseHarness {
  static void say(String k, Object v) { System.out.println(k + "\t" + v); }
  public static void main(String[] a) {
    // What survives cleaning.
    say("commas",   Dial.clean("+1 (800) 555-0100,,123#"));
    say("wait",     Dial.clean("+18005550100;123#"));
    say("p",        Dial.clean("8005550100p1234"));
    say("w",        Dial.clean("8005550100w1234"));
    say("PW",       Dial.clean("8005550100P1234W99"));
    // A letter is a pause only BETWEEN dialable characters: a street is not a phone tree.
    say("street",   Dial.clean("555 Powell St"));
    say("trailing", Dial.clean("5550100p"));
    say("name",     Dial.clean("call Alice 555"));

    // What the pad can type.
    say("held-star",    (int) Dial.held('*', true));
    say("held-hash",    (int) Dial.held('#', true));
    say("held-zero",    (int) Dial.held('0', true));
    say("held-one",     (int) Dial.held('1', true));
    // The in-call pad sends DTMF down a live call, where a pause means nothing.
    say("held-star-tones", (int) Dial.held('*', false));
    say("held-hash-tones", (int) Dial.held('#', false));
    say("held-zero-tones", (int) Dial.held('0', false));

    say("press-pause", Dial.press("5550100", Dial.PAUSE));
    say("press-wait",  Dial.press("5550100", Dial.WAIT));
    say("press-p",     Dial.press("5550100", 'p'));
    say("press-w",     Dial.press("5550100", 'w'));

    // The URI. `#` is a fragment separator: unencoded, everything after it is thrown away.
    say("uri",      Dial.telUri("+1 (800) 555-0100,,123#"));
    say("uri-wait", Dial.telUri("+18005550100;123#"));
    say("uri-mmi",  Dial.telUri("*21*15550100#"));
    say("uri-plain", Dial.telUri("5550100"));

    say("dialable", Dial.dialable("+18005550100,,123#") + " " + Dial.dialable(",,;;"));
  }
}
"""


@unittest.skipIf(not JAVAC or not JAVARUN, "no JDK on this node")
@unittest.skipIf(not os.path.isdir(PHONE), "no android sources here")
class APauseSurvivesWhatTheDialerDoesToIt(unittest.TestCase):
    """`Dial` is pure, so every case below is EXECUTED rather than asserted about."""

    out = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        h = os.path.join(cls.tmp, "PauseHarness.java")
        with open(h, "w") as f:
            f.write(HARNESS)
        r = subprocess.run(
            [JAVAC, "-nowarn", "-d", cls.tmp, "-sourcepath", ac.JAVA,
             os.path.join(PHONE, "Dial.java"), h],
            capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]
        r = subprocess.run([JAVARUN, "-cp", cls.tmp, "PauseHarness"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-4000:]
        cls.out = dict(line.split("\t", 1) for line in r.stdout.splitlines() if "\t" in line)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_pause_characters_reach_the_radio(self):
        self.assertEqual(self.out["commas"], "+18005550100,,123#")
        self.assertEqual(self.out["wait"], "+18005550100;123#")

    def test_p_and_w_are_the_same_two_pauses_spelled_differently(self):
        """A vCard from another phone, or a carrier's own printed card, writes them this way.
        Dropped as letters the extension ran straight into the number: `8005550100p1234` became
        `80055501001234`, which is not a failure — it is a different number, dialled."""
        self.assertEqual(self.out["p"], "8005550100,1234")
        self.assertEqual(self.out["w"], "8005550100;1234")
        self.assertEqual(self.out["PW"], "8005550100,1234;99")

    def test_a_letter_in_a_name_is_still_a_letter(self):
        """The other half of the same rule, and the reason it checks both neighbours: `555 Powell
        St` is an address. Read as a pause its `P` turns a street into `555,`."""
        self.assertEqual(self.out["street"], "555")
        self.assertEqual(self.out["trailing"], "5550100")
        self.assertEqual(self.out["name"], "555")

    def test_a_long_press_on_star_and_hash_types_the_two_pauses(self):
        """THE BUG AS REPORTED. The pad wired one long press — `+` on zero — so the only way a
        pause could ever enter this app was a contact card somebody made elsewhere."""
        self.assertEqual(self.out["held-star"], str(ord(",")))
        self.assertEqual(self.out["held-hash"], str(ord(";")))
        self.assertEqual(self.out["held-zero"], str(ord("+")))
        self.assertEqual(self.out["held-one"], "0", "holding 1 calls voicemail; it types nothing")

    def test_the_in_call_pad_offers_no_pause(self):
        """It sends DTMF down a live call, where a pause is not a thing that can happen. A key that
        types one there is a key that does nothing, and a hint under it is a lie."""
        self.assertEqual(self.out["held-star-tones"], "0")
        self.assertEqual(self.out["held-hash-tones"], "0")
        self.assertEqual(self.out["held-zero-tones"], str(ord("+")),
                         "`+` is not a pause and must survive on the in-call pad")

    def test_a_pause_can_be_appended_however_it_is_spelled(self):
        self.assertEqual(self.out["press-pause"], "5550100,")
        self.assertEqual(self.out["press-wait"], "5550100;")
        self.assertEqual(self.out["press-p"], "5550100,")
        self.assertEqual(self.out["press-w"], "5550100;")

    def test_the_hash_is_percent_encoded_or_the_extension_is_thrown_away(self):
        """`Uri.parse("tel:+18005550100,,123#")` keeps `+18005550100,,123` and reads the rest as a
        FRAGMENT. The platform then dials a different number and nothing anywhere says so."""
        self.assertEqual(self.out["uri"], "tel:%2B18005550100%2C%2C123%23")
        self.assertEqual(self.out["uri-wait"], "tel:%2B18005550100%3B123%23")
        self.assertEqual(self.out["uri-mmi"], "tel:*21*15550100%23")

    def test_an_ordinary_number_is_not_mangled_by_the_encoder(self):
        self.assertEqual(self.out["uri-plain"], "tel:5550100")

    def test_a_number_with_pauses_is_still_worth_dialling(self):
        self.assertEqual(self.out["dialable"], "true false")


@unittest.skipIf(not os.path.isdir(PHONE), "no android sources here")
class TheWiringThatCannotBeRun(unittest.TestCase):
    """Keypad builds Views and PcInCallService extends InCallService, so neither can be executed
    here. What is checked is the one line in each that decides the behaviour."""

    @classmethod
    def setUpClass(cls):
        cls.src = {}
        for f in ("Keypad.java", "DialerActivity.java", "InCallActivity.java",
                  "PcInCallService.java"):
            cls.src[f] = open(os.path.join(PHONE, f), encoding="utf-8").read()

    @staticmethod
    def _call(src, head):
        """The argument text of `head(...)`, read by balancing brackets rather than by a regex.

        The call spans an anonymous class full of `;` and `)`, so a pattern loose enough to reach
        the end of it runs straight past into the next statement — which is how the first version of
        this check reported the dialer's permission constant as the pause flag."""
        at = src.index(head)
        i = src.index("(", at)
        depth, j = 0, i
        while j < len(src):
            if src[j] == "(":
                depth += 1
            elif src[j] == ")":
                depth -= 1
                if depth == 0:
                    return src[i + 1:j]
            j += 1
        raise AssertionError("unbalanced call: " + head)

    def test_the_dialers_pad_is_built_with_the_pause_keys(self):
        src = self.src["DialerActivity.java"]
        self.assertIn("Keypad.build(this, pad, pal, keySizeDp()", src,
                      "the dialer no longer builds its pad — nothing can be typed at all")
        args = self._call(src, "Keypad.build(this, pad, pal, keySizeDp()")
        self.assertTrue(args.rstrip().endswith(", true"),
                        "the pad is built without the pause keys: `,` and `;` cannot be typed "
                        "anywhere in this app — last argument was " + repr(args.rstrip()[-24:]))

    def test_the_in_call_pad_is_not(self):
        r"""Read by BALANCING BRACKETS, for the reason `_call` exists.

        The first version of this was `assertNotRegex(src, r"Keypad\.build\([^;]*,\s*true\s*\)")`
        and it could not fail: the call spans an anonymous class whose body contains a `;`, so
        `[^;]*` never reaches the argument list's end and the pattern does not match the MUTATED
        source either. A guard that passes on the exact change it exists to catch is not a guard.
        """
        args = self._call(self.src["InCallActivity.java"], "Keypad.build(this, pad, pal, 52")
        self.assertFalse(args.rstrip().endswith(", true"),
                         "the DTMF pad offers a pause, which does nothing on a live call")

    def test_the_long_press_is_wired_to_what_dial_decided(self):
        self.assertIn("Dial.held(", self.src["Keypad.java"],
                      "Keypad grew its own copy of the rule; only Dial's can be run by a test")
        self.assertIn("press.onKey(held)", self.src["Keypad.java"],
                      "a key that decides what it holds and never delivers it")

    def test_a_wait_is_answered(self):
        """`onPostDialWait` is the ONLY place the platform asks, and `postDialContinue` the only
        way to answer. Without both, everything after a `;` is silently never sent."""
        self.assertIn("onPostDialWait", self.src["PcInCallService.java"])
        self.assertIn("postDialContinue", self.src["PcInCallService.java"])
        self.assertIn("PcInCallService.postDialContinue(true)", self.src["InCallActivity.java"],
                      "nothing on the call screen can say 'send the rest'")
        self.assertIn("PcInCallService.postDialContinue(false)", self.src["InCallActivity.java"],
                      "a wait that is never answered lasts as long as the call")

    def test_leaving_the_screen_for_a_moment_does_not_throw_the_rest_away(self):
        """ANSWERING "no" IS UNRECOVERABLE — telephony asks once, and there is no second ask.

        `onStop` used to `cancel()` the dialog unconditionally, and cancel fires the listener that
        answers "not now". So pressing HOME while a phone tree connected — or anything else that
        stops this activity — silently abandoned the extension, and coming back showed an ordinary
        call with no dialog and nothing to say why it had stopped halfway through the number. The
        dialog still always goes (a window outliving its activity is leaked); only a screen that is
        really going answers, and `onStart`'s `draw()` asks again for one that is coming back."""
        src = self.src["InCallActivity.java"]
        stop = src[src.index("protected void onStop()"):src.index("private final Runnable tick")]
        self.assertIn("waitAsk.dismiss()", stop,
                      "a transient stop must dismiss the dialog without answering for the user")
        self.assertIn("isFinishing()", stop,
                      "onStop cannot tell 'going away' from 'coming back', so it answers for both")

    def test_the_tel_uri_is_encoded_by_the_code_a_test_can_run(self):
        self.assertIn("Dial.telUri(", self.src["DialerActivity.java"])
        self.assertNotIn('"tel:" + Uri.encode(', self.src["DialerActivity.java"])


@unittest.skipIf(ac.android_jar() is None, "no android.jar on this node")
@unittest.skipIf(not JAVAC, "no JDK on this node")
class ItAllStillCompiles(unittest.TestCase):
    """A rule proved on a pure class and a screen that no longer builds is not a fixed phone.

    DialerActivity itself is NOT here: it reaches MainActivity (Capacitor), LaunchView and SmsRoutes,
    which between them drag okhttp, androidx.media and half the app onto a box that has none of it.
    Shimming that far would leave nothing genuinely compiled. Its one changed line is pinned by
    `TheWiringThatCannotBeRun` above and built for real by the CI APK job.
    """

    def test_the_phone_screens_compile_against_the_real_sdk(self):
        tmp = tempfile.mkdtemp()
        try:
            src = [os.path.join(PHONE, f) for f in
                   ("Dial.java", "Keypad.java", "CallRules.java",
                    "PcInCallService.java", "InCallActivity.java")]
            r = ac.compile_sources(src, tmp)
            self.assertEqual(r.returncode, 0, r.stderr[-4000:])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
