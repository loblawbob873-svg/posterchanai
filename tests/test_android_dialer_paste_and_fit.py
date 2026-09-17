"""THE PHONE APP: keys that fit, a number you can paste, and a number other apps can hand over.

Three reports, one message: "the number circle buttons are cut off at the top and bottom of that row,
also, no way to paste a number, no way for other apps to open number in the phone app".

  * CLIPPED KEYS. The keys were sized from the whole screen minus a guessed 300dp of chrome. The
    real chrome (header, call row, tab bar, system bars, sometimes a notice) is more than that, so
    four rows came out taller than the box the layout gave them; the box centres its content and
    the top row lost its top, the bottom row its bottom. The size now comes from `PadFit`, run
    against the MEASURED box.
  * PASTE. The number was `textIsSelectable`, which owns the long press and offers Copy only.
  * OTHER APPS. A `tel:` LINK already opened here. Plain text that is a number did not: there was no
    PROCESS_TEXT entry (the text-selection menu) and no SEND target (the share sheet).

Paste, selection and share all hand over TEXT, never a number, so `Dial.fromText` pulls the number
out of it. That rule and the fit are RUN here; the wiring that cannot run off a device is read, and
DialerDeviceTest measures the real layout on the emulator.
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
LAYOUT = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "res", "layout", "tel_dialer.xml")
JAVAC = shutil.which("javac")
JAVARUN = shutil.which("java")

TEXTS = [
    ("sentence", "Call me on 555-010-4477 after 5"),
    ("plain", "+1 (555) 010-4477"),
    ("full-stop", "It is 5550104477."),
    ("time-first", "12:30 at 555-0100"),
    ("extension", "+1 800 555 0100 p 1234"),
    ("ext-letter", "18005550100p1234"),
    ("pm", "ring 5550100 at 5pm"),
    ("trailing-comma", "call 5550100, then hang up"),
    ("pause-words", "code 1234 pw 5550100"),
    ("service", "dial *#06# to see it"),
    ("short", "press 911"),
    ("nothing", "see you tomorrow"),
    ("empty", ""),
    ("tel-link", "tel:+15550104477"),
    ("two-lines", "home 555 0100\nwork 555 0199"),
    ("en-dash", "555–010–4477"),
]

HARNESS = r"""
import place.poster.app.phone.Dial;
import place.poster.app.phone.PadFit;

public class PasteHarness {
  static void say(String k, Object v) { System.out.println(k + "\t" + v); }
  public static void main(String[] a) throws Exception {
    java.io.BufferedReader in = new java.io.BufferedReader(
        new java.io.InputStreamReader(System.in, "UTF-8"));
    String line;
    while ((line = in.readLine()) != null) {
      int t = line.indexOf('\t');
      String text = line.substring(t + 1).replace("\\n", "\n");
      say("text:" + line.substring(0, t), "[" + Dial.fromText(text) + "]");
    }
    say("text:null", "[" + Dial.fromText(null) + "]");
    // Every box from a watch to a tablet: report any fit that overflows.
    int bad = 0, checked = 0;
    for (int w = 120; w <= 900; w += 7) {
      for (int h = 150; h <= 1200; h += 9) {
        for (int n = 40; n <= 90; n += 25) {
          int k = PadFit.keyDp(w, h, n);
          checked++;
          if (k > PadFit.MAX_KEY || k < PadFit.MIN_KEY) bad++;
          else if (k > PadFit.MIN_KEY && (3 * PadFit.cellDp(k) > w || 4 * PadFit.cellDp(k) + n > h)) bad++;
          else if (k < PadFit.MAX_KEY && 3 * PadFit.cellDp(k + 1) <= w && 4 * PadFit.cellDp(k + 1) + n <= h) bad++;
        }
      }
    }
    say("fit-bad", bad + "/" + checked);
    say("fit-roomy", PadFit.keyDp(411, 700, 67));
    say("fit-tight", PadFit.keyDp(360, 450, 67));
    say("fit-tiny", PadFit.keyDp(200, 120, 67));
    say("margin", PadFit.marginDp(70) + " " + PadFit.marginDp(69));
  }
}
"""


def _old_key_dp(wdp, hdp):
    """The formula that shipped before, verbatim, so the test shows what it did."""
    by_width = (wdp - 40) // 3 - 18
    by_height = (hdp - 300) // 4 - 18
    return max(52, min(88, min(by_width, by_height)))


def _cell(key):
    return key + 2 * (9 if key >= 70 else 7)


@unittest.skipIf(not JAVAC or not JAVARUN, "no JDK on this node")
@unittest.skipIf(not os.path.isdir(PHONE), "no android sources here")
class PasteAndFitRules(unittest.TestCase):
    out = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        h = os.path.join(cls.tmp, "PasteHarness.java")
        with open(h, "w") as f:
            f.write(HARNESS)
        src = [os.path.join(PHONE, "Dial.java"), os.path.join(PHONE, "PadFit.java")]
        r = subprocess.run([JAVAC, "-encoding", "UTF-8", "-nowarn", "-d", cls.tmp] + src + [h],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]
        stdin = "".join("%s\t%s\n" % (k, v.replace("\n", "\\n")) for k, v in TEXTS)
        r = subprocess.run([JAVARUN, "-Dfile.encoding=UTF-8", "-cp", cls.tmp, "PasteHarness"],
                           input=stdin.encode("utf-8"), capture_output=True, timeout=120)
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-4000:]
        cls.out = dict(line.split("\t", 1) for line in r.stdout.decode("utf-8").splitlines() if "\t" in line)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def got(self, key):
        return self.out["text:" + key]

    def test_the_number_is_taken_out_of_the_sentence_around_it(self):
        """Every digit of "Call me on 555-010-4477 after 5" is not a number; the 5 is a time."""
        self.assertEqual(self.got("sentence"), "[5550104477]")
        self.assertEqual(self.got("plain"), "[+15550104477]")
        self.assertEqual(self.got("full-stop"), "[5550104477]")
        self.assertEqual(self.got("tel-link"), "[+15550104477]")
        self.assertEqual(self.got("en-dash"), "[5550104477]")

    def test_a_time_before_the_number_is_skipped(self):
        self.assertEqual(self.got("time-first"), "[5550100]")
        self.assertEqual(self.got("pm"), "[5550100]")
        # Prose punctuation after a number is not a pause.
        self.assertEqual(self.got("trailing-comma"), "[5550100]")
        # `p`/`w` are pauses only BETWEEN digits; as letters of a word they end the run.
        self.assertEqual(self.got("pause-words"), "[5550100]")

    def test_an_extension_keeps_its_pause(self):
        self.assertEqual(self.got("ext-letter"), "[18005550100,1234]")
        # Spaced out, the `p` is a word between numbers and not a pause; the number itself survives.
        self.assertTrue(self.got("extension").startswith("[+18005550100"), self.got("extension"))

    def test_a_service_code_and_a_short_number_still_come_through(self):
        self.assertEqual(self.got("service"), "[*#06#]")
        self.assertEqual(self.got("short"), "[911]")

    def test_the_first_of_two_numbers_wins_and_lines_never_join(self):
        self.assertEqual(self.got("two-lines"), "[5550100]")

    def test_text_with_no_number_leaves_the_pad_empty(self):
        """An empty pad is honest; stray digits strung together are somebody else's number."""
        self.assertEqual(self.got("nothing"), "[]")
        self.assertEqual(self.got("empty"), "[]")
        self.assertEqual(self.out["text:null"], "[]")

    def test_the_fit_never_overflows_and_never_wastes_room(self):
        """Over every box from a watch to a tablet: four rows plus the number fit, and one dp bigger
        would not have (unless already at the limits)."""
        bad, checked = self.out["fit-bad"].split("/")
        self.assertGreater(int(checked), 1000)
        self.assertEqual(bad, "0")
        self.assertEqual(self.out["margin"], "9 7", "Keypad and PadFit disagree about a key's room")

    def test_the_short_phone_that_clipped_now_fits(self):
        """A 360x740dp phone: the pad's box is ~450dp once the header, number, call row, tab bar and
        system bars are paid for. The old formula made 88dp keys (4 rows = 424dp + a 67dp number =
        491dp): 41dp too tall, split between the top row and the bottom row."""
        old = _old_key_dp(360, 740)
        self.assertGreater(4 * _cell(old) + 67, 450, "the premise: the shipped formula overflowed")
        fit = int(self.out["fit-tight"])
        self.assertLessEqual(4 * _cell(fit) + 67, 450)
        self.assertEqual(self.out["fit-roomy"], "88")
        self.assertEqual(self.out["fit-tiny"], str(44))


@unittest.skipIf(not os.path.isdir(PHONE), "no android sources here")
class TheWiring(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        def code(name):
            src = open(os.path.join(PHONE, name), encoding="utf-8").read()
            return src
        cls.dialer = code("DialerActivity.java")
        cls.keypad = code("Keypad.java")
        cls.man = open(MANIFEST, encoding="utf-8").read()
        cls.layout = open(LAYOUT, encoding="utf-8").read()

    def _method(self, head):
        i = self.dialer.index(head)
        j = self.dialer.index("{", i)
        depth = 0
        for k in range(j, len(self.dialer)):
            if self.dialer[k] == "{":
                depth += 1
            elif self.dialer[k] == "}":
                depth -= 1
                if depth == 0:
                    return self.dialer[i:k]
        raise AssertionError(head)

    def test_the_keys_are_sized_from_the_measured_box(self):
        fit = self._method("private void fitPad()")
        self.assertIn("PadFit.keyDp(", fit)
        self.assertIn("padWrap.getHeight()", fit)
        self.assertIn("buildPad()", fit)
        self.assertIn("padWrap.addOnLayoutChangeListener", self.dialer)
        self.assertIn("this::fitPad", self.dialer)
        self.assertIn("if (padKeyDp > 0) return padKeyDp;", self._method("private int keySizeDp()"))
        self.assertIn("PadFit.marginDp(size)", self.keypad)

    def test_the_number_can_be_pasted(self):
        num = self.layout[self.layout.index('android:id="@+id/pc_dl_number"'):]
        num = num[:num.index("/>")]
        self.assertNotIn("textIsSelectable", num, "selection owns the long press, so Paste never shows")
        self.assertIn('android:id="@+id/pc_dl_paste"', self.layout)
        self.assertIn("pasteBtn.setOnClickListener(v -> pasteNumber())", self.dialer)
        self.assertIn("numberView.setOnLongClickListener", self.dialer)
        paste = self._method("private void pasteNumber()")
        self.assertIn("getPrimaryClip()", paste)
        self.assertIn("Dial.fromText(", paste)
        draw = self._method("private void drawNumber()")
        self.assertIn("clipboardHasText()", draw)
        # Deciding whether to SHOW Paste must not read the contents (Android 12+ announces a read).
        self.assertNotIn("getPrimaryClip()", self._method("private boolean clipboardHasText()"))

    def test_another_app_can_hand_over_a_selection_or_a_share(self):
        i = self.man.index('android:name=".phone.CallFromText"')
        start = self.man.rindex("<activity-alias", 0, i)
        block = self.man[start:self.man.index("</activity-alias>", i)]
        self.assertIn('android:targetActivity=".phone.DialerActivity"', block)
        self.assertIn('android:enabled="false"', block, "every text selection on the phone, uninvited")
        self.assertIn("android.intent.action.PROCESS_TEXT", block)
        self.assertIn("android.intent.action.SEND", block)
        self.assertIn('android:mimeType="text/plain"', block)
        read = self._method("private void readIntent(Intent i)")
        self.assertIn("EXTRA_PROCESS_TEXT", read)
        self.assertIn("Intent.EXTRA_TEXT", read)
        self.assertIn("Dial.fromText(", read)
        self.assertIn('"place.poster.app.phone.CallFromText"', self._method("private void offerCallFromText()"))
        self.assertIn("offerCallFromText();", self._method("protected void onCreate(Bundle saved)"))

    def test_a_tel_link_still_prefills_and_never_dials(self):
        read = self._method("private void readIntent(Intent i)")
        self.assertIn("typed = Dial.clean(raw)", read)
        self.assertNotIn("placeCall", read)
        self.assertNotIn("place(", read)


if __name__ == "__main__":
    unittest.main()
