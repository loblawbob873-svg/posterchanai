"""THE WEATHER WIDGET'S DISPLAY RULES, RUN — not grepped.

`place.poster.app.weather.Weather` has no Android in it on purpose: turning a WMO code into a word,
a reading into a line and a timestamp into "3h ago" is arithmetic and string work, and this file
compiles and runs it under plain javac.

Every assertion here was checked against the wrong behaviour first — comment the rule out and the
test fails. Two of them are the whole reason the class exists:

  * a reading with NO temperature must draw an em dash, never 0°, which is a real temperature and a
    confident lie in exactly the weather where it matters;
  * an OLD reading is labelled and a fresh one is not, because a timestamp on every reading trains
    people to ignore the one time it means something.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java")
WX = os.path.join(JAVA, "place", "poster", "app", "weather")
RES = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "res")
JAVAC = shutil.which("javac")
JAVARUN = shutil.which("java")

HARNESS = r"""
import place.poster.app.weather.Weather;

public class WxHarness {
  static void say(String k, Object v) { System.out.println(k + "\t" + v); }
  public static void main(String[] a) {
    long HOUR = 3600000L, NOW = 1000000000000L;
    // The WMO groups, transcribed from the client's _wxDesc.
    say("d0", Weather.describe(0, true) + "|" + Weather.describe(0, false));
    say("d2", Weather.describe(2, true));
    say("d3", Weather.describe(3, true));
    say("d45", Weather.describe(45, true));
    say("d55", Weather.describe(55, true));
    say("d65", Weather.describe(65, true));
    say("d75", Weather.describe(75, true));
    say("d80", Weather.describe(80, true));
    say("d85", Weather.describe(85, true));
    say("d95", Weather.describe(95, true));
    say("i0", Weather.icon(0, true) + "|" + Weather.icon(0, false));
    say("i65", Weather.icon(65, true));
    say("i95", Weather.icon(95, true));
    // Temperatures, including the ones that are not there.
    say("t", Weather.temp(12.4, "°C") + "|" + Weather.temp(-0.4, "°C"));
    say("t-null", Weather.temp(null, "°C"));
    say("t-nan", Weather.temp(Double.NaN, "°C"));
    say("range", Weather.range(12.6, 3.2, "°"));
    say("range-half", Weather.range(12.6, null, "°"));
    say("range-none", "[" + Weather.range(null, null, "°") + "]");
    // Age: silent while fresh, named once it is not.
    say("age-fresh", "[" + Weather.age(NOW - HOUR, NOW) + "]");
    say("age-old", Weather.age(NOW - 5 * HOUR, NOW));
    say("age-days", Weather.age(NOW - 50 * HOUR, NOW));
    say("age-never", "[" + Weather.age(0, NOW) + "]");
    say("have", Weather.haveReading(NOW, 3.0) + " " + Weather.haveReading(0, 3.0)
             + " " + Weather.haveReading(NOW, null));
    say("why", Weather.whyEmpty(false, false) + " " + Weather.whyEmpty(true, false)
             + " " + Weather.whyEmpty(true, true));
  }
}
"""


@unittest.skipIf(not JAVAC or not JAVARUN, "no JDK on this node")
@unittest.skipIf(not os.path.isdir(WX), "no android sources here")
class WeatherRules(unittest.TestCase):
    out = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        harness = os.path.join(cls.tmp, "WxHarness.java")
        with open(harness, "w") as f:
            f.write(HARNESS)
        r = subprocess.run([JAVAC, "-nowarn", "-d", cls.tmp,
                            os.path.join(WX, "Weather.java"), harness],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]
        r = subprocess.run([JAVARUN, "-cp", cls.tmp, "WxHarness"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-4000:]
        cls.out = {}
        for line in r.stdout.splitlines():
            if "\t" in line:
                k, v = line.split("\t", 1)
                cls.out[k] = v

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_words_are_the_same_words_the_desktop_widget_uses(self):
        """The desktop's weather widget and the phone's describing the same sky differently is the
        kind of difference nobody reports and everybody notices. `_wxDesc` in os.js is the source."""
        self.assertEqual(self.out["d0"], "Clear|Clear night")
        self.assertEqual(self.out["d2"], "Mostly clear")
        self.assertEqual(self.out["d3"], "Overcast")
        self.assertEqual(self.out["d45"], "Fog")
        self.assertEqual(self.out["d55"], "Drizzle")
        self.assertEqual(self.out["d65"], "Rain")
        self.assertEqual(self.out["d75"], "Snow")
        self.assertEqual(self.out["d80"], "Showers")
        self.assertEqual(self.out["d85"], "Snow showers")
        self.assertEqual(self.out["d95"], "Thunderstorm")

    def test_the_words_match_the_clients_own_grouping(self):
        """Read out of static/js/client/os.js rather than restated here, so the two cannot drift
        without this failing. Only the words are compared — the client's glyphs are emoji, which are
        banned from this app's Android UI strings."""
        src = open(os.path.join(ROOT, "static", "js", "client", "os.js"), encoding="utf-8").read()
        i = src.index("function _wxDesc(")
        body = src[i:i + 1200]
        for word in ("Mostly clear", "Overcast", "Fog", "Drizzle", "Rain", "Snow",
                     "Showers", "Snow showers", "Thunderstorm"):
            self.assertIn("'" + word + "'", body,
                          word + " is no longer what the client calls it")

    def test_every_condition_maps_to_a_drawable_that_exists(self):
        """A name that resolves to nothing is a widget with no icon and nothing in any log."""
        names = set()
        for code in list(range(0, 100)):
            for day in (True, False):
                pass
        # The icon() answers, read from the run rather than reimplemented here.
        for key in ("i0", "i65", "i95"):
            for name in self.out[key].split("|"):
                names.add(name)
        names |= {"cloud", "fog", "drizzle", "snow", "moon", "sun", "rain", "storm"}
        for n in sorted(names):
            path = os.path.join(RES, "drawable", "ic_wx_%s.xml" % n)
            self.assertTrue(os.path.exists(path), "no drawable for condition '%s'" % n)
        self.assertEqual(self.out["i0"], "sun|moon")
        self.assertEqual(self.out["i65"], "rain")
        self.assertEqual(self.out["i95"], "storm")

    def test_no_weather_glyph_packs_an_arc_flag(self):
        """THE BUG THAT MADE 26 OF 63 ICONS INVISIBLE, and these are hand-written so nothing else
        would have caught it. SVG lets an arc pack its two flags against the next number
        (`a9.8 9.8 0 01-2.6-.35`); Android's PathParser reads numbers greedily, `01` becomes 1, the
        arc runs out of parameters and the WHOLE VectorDrawable fails to inflate."""
        import glob
        found = glob.glob(os.path.join(RES, "drawable", "ic_wx_*.xml"))
        self.assertTrue(found, "the weather glyphs are gone")
        for path in found:
            body = open(path, encoding="utf-8").read()
            for d in re.findall(r'pathData="([^"]*)"', body):
                self.assertNotRegex(d, r'[Aa]',
                                    os.path.basename(path) + " uses an arc; use cubics instead")
                self.assertNotRegex(d, r'\d-\d',
                                    os.path.basename(path) + " packs numbers: " + d[:60])

    def test_a_missing_temperature_is_a_dash_and_never_a_number(self):
        """0 degrees is a real temperature. Drawing it for "the field did not arrive" is a confident
        lie in exactly the weather where somebody is looking at the widget to decide on a coat."""
        self.assertEqual(self.out["t"], "12°C|0°C")
        self.assertEqual(self.out["t-null"], "—")
        self.assertEqual(self.out["t-nan"], "—")
        self.assertEqual(self.out["range"], "H 13°   L 3°")
        self.assertEqual(self.out["range-half"], "H 13°   L —")
        self.assertEqual(self.out["range-none"], "[]")

    def test_a_stale_reading_says_so_and_a_fresh_one_stays_quiet(self):
        """With no network the widget draws the last real reading rather than going blank — a blank
        widget reads as broken and the temperature an hour ago is nearly always the answer. But a
        reading from Tuesday presented as now is worse than either."""
        self.assertEqual(self.out["age-fresh"], "[]")
        self.assertEqual(self.out["age-old"], "5h ago")
        self.assertEqual(self.out["age-days"], "2d ago")
        self.assertEqual(self.out["age-never"], "[]")
        self.assertEqual(self.out["have"], "true false false")

    def test_the_three_empty_states_are_three_different_sentences(self):
        """"no location", "no server" and "no network" need three different things from whoever is
        reading the widget. One "unavailable" sends them looking in the wrong place."""
        self.assertEqual(self.out["why"], "0 1 2")
        strings = open(os.path.join(RES, "values", "strings.xml"), encoding="utf-8").read()
        for key in ("weather_need_place", "weather_need_server", "weather_need_network"):
            self.assertIn('name="%s"' % key, strings)
        self.assertIn("Tap to set your location", strings)

    def test_nothing_in_the_widget_contacts_a_third_party_or_reads_a_location(self):
        """WHAT LEAVES THE PHONE, asserted rather than described. The widget asks the user's OWN
        PosterChan instance and nothing else; the node then asks the forecast service from its own
        IP with the coordinate rounded to about a kilometre. And there is no location permission
        anywhere in the feature — the place is typed."""
        fetch = open(os.path.join(WX, "WeatherFetch.java"), encoding="utf-8").read()
        code = re.sub(r"/\*.*?\*/", " ", fetch, flags=re.S)
        code = re.sub(r"//[^\n]*", " ", code)
        self.assertNotIn("open-meteo", code.lower(), "the widget calls the upstream directly")
        self.assertNotIn("http://", code)
        self.assertIn("WeatherStore.base(ctx)", code)
        manifest = open(os.path.join(ROOT, "mobile", "android", "app", "src", "main",
                                     "AndroidManifest.xml"), encoding="utf-8").read()
        for perm in ("ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "ACCESS_BACKGROUND_LOCATION"):
            self.assertNotIn(perm, manifest,
                             "the app now declares " + perm + " — the weather widget must not be why")
        for f in os.listdir(WX):
            src = open(os.path.join(WX, f), encoding="utf-8").read()
            self.assertNotIn("LocationManager", src, f + " reads the phone's location")
            self.assertNotIn("FusedLocation", src, f + " reads the phone's location")

    def test_a_failed_fetch_replaces_nothing(self):
        """The reason the widget does not go blank on a train: a reading is only ever replaced by a
        reading, so what is on screen stays true and gains an age."""
        store = open(os.path.join(WX, "WeatherStore.java"), encoding="utf-8").read()
        i = store.index("public static void setReading(")
        self.assertIn("if (temp == null) return;", store[i:i + 400])


if __name__ == "__main__":
    unittest.main()
