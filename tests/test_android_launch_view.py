"""A tile opens the screen it names, on a phone that already had the app open.

    "on tablet, email app is loading News!"
    "same for other apps"

The second line is the shape of the bug, not a second report. Every PosterChan tile and every drawer
alias went through one of two identical intent builders, so they failed together and identically: the
app came forward showing whatever it had been showing last.

The channel was an intent extra, and an extra only arrives if the intent is DELIVERED. Both builders
dressed their intent as a launcher press — ACTION_MAIN plus CATEGORY_LAUNCHER — at an activity
declared singleTask. That is the intent the system sends when somebody taps a home-screen icon, and
its contract is "bring this app back the way I left it". Nothing threw, nothing logged, the app
animated forward perfectly, and the only symptom was the wrong screen — indistinguishable from a tile
wired to the wrong slug, which is where the reading time went.

Two halves are checked here, because either alone passes while the bug is present:
  * LaunchView is RUN (javac + java), including the staleness and consume-once rules;
  * neither launch path dresses its intent as a launcher press again.
"""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "mobile/android/app/src/main/java/place/poster/app"
HOME = JAVA / "home"

HAVE_JDK = shutil.which("javac") and shutil.which("java")

HARNESS = r"""
import place.poster.app.home.LaunchView;

public class Harness {
    static int failed = 0;
    static void ok(String what, boolean cond) {
        if (!cond) { failed++; System.out.println("FAIL " + what); }
        else System.out.println("ok   " + what);
    }

    public static void main(String[] a) {
        long t = 1_000_000_000L;

        // The whole point: a parked request comes back.
        LaunchView.request("mail", t);
        ok("a parked view is returned", "mail".equals(LaunchView.take(t + 5)));

        // ...and only once. An unconsumed request is re-performed on every later resume, which is
        // how a press yanks somebody back to Email long after they moved on.
        LaunchView.request("mail", t);
        LaunchView.take(t + 5);
        ok("reading consumes it", "".equals(LaunchView.take(t + 6)));

        // A minute-old request is somebody who has moved on. Opening Email over what they are now
        // doing is a worse answer than opening nothing.
        LaunchView.request("notes", t);
        ok("a stale request is dropped", "".equals(LaunchView.take(t + LaunchView.MAX_AGE_MS)));
        LaunchView.request("notes", t);
        ok("just inside the window is kept",
           "notes".equals(LaunchView.take(t + LaunchView.MAX_AGE_MS - 1)));

        // A clock that went backwards must not make a fresh request look like it arrived from the
        // future and get performed for ever after.
        LaunchView.request("notes", t);
        ok("a backwards clock drops rather than latches", "".equals(LaunchView.take(t - 10)));

        // The newest press wins: two taps in a row open the second thing, not the first.
        LaunchView.request("mail", t);
        LaunchView.request("news", t + 1);
        ok("the newest request wins", "news".equals(LaunchView.take(t + 2)));

        // "Open the app, no particular screen" must ERASE a previous request, or the last tile
        // pressed would be re-opened by a plain app launch.
        LaunchView.request("mail", t);
        LaunchView.clear();
        ok("clear erases a parked request", "".equals(LaunchView.take(t + 1)));
        LaunchView.request("mail", t);
        LaunchView.request("", t + 1);
        ok("an empty request erases too", "".equals(LaunchView.take(t + 2)));

        // Nothing parked is "", never null — the plugin puts this straight into a JSObject.
        ok("empty is a string", "".equals(LaunchView.take(t)));

        LaunchView.request("  mail  ", t);
        ok("a view is trimmed", "mail".equals(LaunchView.take(t + 1)));

        LaunchView.request(null, t);
        ok("null is not a request", "".equals(LaunchView.take(t + 1)));

        ok("waiting() does not consume",
           LaunchView.waiting(t) == false);
        LaunchView.request("mail", t);
        ok("waiting() sees a fresh request", LaunchView.waiting(t + 1));
        ok("waiting() left it alone", "mail".equals(LaunchView.take(t + 2)));

        System.out.println(failed == 0 ? "ALL OK" : (failed + " FAILED"));
        if (failed != 0) System.exit(1);
    }
}
"""


@unittest.skipUnless(HAVE_JDK, "javac/java not installed")
class LaunchViewRuns(unittest.TestCase):
    def test_the_handoff_behaves(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            pkg = d / "place/poster/app/home"
            pkg.mkdir(parents=True)
            shutil.copy(HOME / "LaunchView.java", pkg / "LaunchView.java")
            (d / "Harness.java").write_text(HARNESS)
            c = subprocess.run(["javac", "-d", str(d), str(pkg / "LaunchView.java"),
                                str(d / "Harness.java")],
                               capture_output=True, text=True, cwd=d)
            self.assertEqual(c.returncode, 0, c.stderr)
            r = subprocess.run(["java", "-cp", str(d), "Harness"],
                               capture_output=True, text=True, cwd=d)
            self.assertIn("ALL OK", r.stdout, r.stdout + r.stderr)


def strip_comments(src):
    """Java source with its comments removed.

    Two recurring ways a guard here passes while the bug is present, both met once each already:
    matching PROSE (this fix's own doc block names CATEGORY_LAUNCHER four times, describing what was
    removed), and matching the FIRST occurrence of a string that appears more than once. Both are
    handled — comments go here, and the method is brace-matched by name below."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == '"':                                  # a string literal may contain // or /*
            j = i + 1
            while j < n and src[j] != '"':
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1]); i = j + 1
        elif src.startswith("//", i):
            i = src.find("\n", i)
            if i < 0:
                break
        elif src.startswith("/*", i):
            i = src.find("*/", i)
            i = n if i < 0 else i + 2
        else:
            out.append(c); i += 1
    return "".join(out)


def method(src, name):
    """The body of one method, brace-matched. A fixed character window is the other way these
    guards rot: `openApp` has been edited twice and `consumeLaunchView` grown twice."""
    i = src.index(name)
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


class TheLaunchIsNotDressedAsALauncherPress(unittest.TestCase):
    """The regression itself. LaunchView can be perfect and the bug still present."""

    # The method that actually starts the app, per file — HomeActivity builds a second, unrelated
    # MainActivity intent up in resolves(), which is what a naive first-match anchor grabbed.
    PATHS = [(HOME / "HomeActivity.java", "private void openApp"),
             (JAVA / "shortcut/ViewActivity.java", "protected void onCreate")]

    def test_warm_launch_event_carries_the_requested_view(self):
        main = (JAVA / "MainActivity.java").read_text()
        plugin = (HOME / "HomePlugin.java").read_text()
        client = (ROOT / "static/js/client/phoneshell.js").read_text()
        self.assertIn("getStringExtra(\n                    place.poster.app.home.HomeActivity.EXTRA_VIEW)", main)
        self.assertIn('o.put("view", requested', plugin)
        self.assertIn("PC.switchView(v)", client)
        self.assertIn("addListener('launchView', launched)", client)

    def _start_block(self, path, fn):
        return method(strip_comments(path.read_text()), fn)

    def test_neither_path_sends_a_home_screen_intent(self):
        for p, fn in self.PATHS:
            with self.subTest(file=p.name):
                block = self._start_block(p, fn)
                self.assertNotIn("CATEGORY_LAUNCHER", block,
                                 "%s dresses its intent as a home-screen press again — a singleTask "
                                 "activity handed that intent is resumed as-was and the extras are "
                                 "discarded" % p.name)
                self.assertNotIn("ACTION_MAIN", block, p.name)

    def test_both_paths_park_the_request(self):
        """The extra alone only survives a cold start."""
        for p, fn in self.PATHS:
            with self.subTest(file=p.name):
                src = self._start_block(p, fn)
                self.assertIn("LaunchView.request(", src,
                              "%s starts the app without parking which view it wants" % p.name)

    def test_the_request_is_parked_before_the_start(self):
        """On a fast device the target resumes and reads before the caller's next line."""
        for p, fn in self.PATHS:
            with self.subTest(file=p.name):
                src = self._start_block(p, fn)
                self.assertLess(src.index("LaunchView.request("), src.index("startActivity(i)"),
                                "%s parks the view after starting the app" % p.name)

    def test_both_carriers_are_kept(self):
        """Cold start has no parked state; warm start drops the extra. Neither alone is enough."""
        for p, fn in self.PATHS:
            with self.subTest(file=p.name):
                block = self._start_block(p, fn)
                self.assertIn("EXTRA_VIEW", block,
                              "%s dropped the intent extra — a COLD start has no process to have "
                              "parked anything" % p.name)


class ThePluginReadsBoth(unittest.TestCase):
    def setUp(self):
        self.body = method(strip_comments((HOME / "HomePlugin.java").read_text()),
                           "public void consumeLaunchView")

    def test_it_prefers_the_parked_request(self):
        self.assertIn("LaunchView.take(", self.body)

    def test_it_still_falls_back_to_the_extra(self):
        self.assertIn("EXTRA_VIEW", self.body)

    def test_it_clears_the_extra_whichever_answered(self):
        """An extra left on the intent is re-read on every later resume."""
        self.assertIn("removeExtra", self.body)

    def test_the_two_agree_on_how_old_is_too_old(self):
        """A hardcoded 60000 beside LaunchView.MAX_AGE_MS is two rules that will drift apart."""
        self.assertIn("LaunchView.MAX_AGE_MS", self.body)
        self.assertNotIn("60000", self.body)


class EveryTileNamesAViewTheClientHas(unittest.TestCase):
    """A slug the client does not know is the same symptom from the other end: the app opens and
    switchView finds nothing, leaving whatever was on screen."""

    def test_the_catalogue_matches_the_sidebar(self):
        cat = (HOME / "HomeTiles.java").read_text()
        views = set(re.findall(r'new Tile\((?:VIEW_[A-Z]+|"([a-z0-9]+)")', cat))
        views.discard("")
        html = (ROOT / "templates" / "client.html").read_text()
        known = set(re.findall(r'data-view="([a-z0-9_]+)"', html))
        # Launcher-only apps need not occupy scarce sidebar space. They are still valid when the
        # shipped renderer explicitly handles them (Music is the first such app).
        app = (ROOT / "static" / "js" / "client" / "app.js").read_text()
        known.update(re.findall(r"VIEW==='([a-z0-9_]+)'", app))
        missing = sorted(v for v in views if v and v not in known)
        self.assertEqual(missing, [],
                         "the launcher offers %r, which the client's sidebar has no view for" % missing)

    def test_the_drawer_aliases_name_known_views(self):
        man = (ROOT / "mobile/android/app/src/main/AndroidManifest.xml").read_text()
        html = (ROOT / "templates" / "client.html").read_text()
        known = set(re.findall(r'data-view="([a-z0-9_]+)"', html))
        named = re.findall(r'android:name="pc\.view"\s+android:value="([a-z0-9_]+)"', man)
        self.assertTrue(named, "no drawer alias declares a view any more — re-read this test")
        for v in named:
            with self.subTest(view=v):
                self.assertIn(v, known)


DOUBLE_HARNESS = r"""
import place.poster.app.home.HomeDoublePress;
public class DoubleHarness {
  static void ok(boolean b, String m){ if(!b) throw new AssertionError(m); }
  public static void main(String[] x){
    HomeDoublePress.clear(); long t=100000;
    ok(!HomeDoublePress.arrived(t), "one press fired");
    ok(HomeDoublePress.arrived(t+300), "intentional pair did not fire");
    ok(!HomeDoublePress.arrived(t+500), "third press replayed the pair");
    HomeDoublePress.clear(); ok(!HomeDoublePress.arrived(t), "first fired");
    ok(HomeDoublePress.arrived(t+1), "batched physical presses did not fire");
    ok(!HomeDoublePress.arrived(t+500), "batched pair replayed");
    HomeDoublePress.clear(); ok(!HomeDoublePress.arrived(t), "first ordinary visit fired");
    ok(!HomeDoublePress.arrived(t+2500), "two ordinary visits became a pair");
    HomeDoublePress.clear(); ok(!HomeDoublePress.arrived(t), "first fired again");
    ok(!HomeDoublePress.arrived(t-1), "backwards clock fired");
    HomeDoublePress.clear(); ok(!HomeDoublePress.clear(), "empty clear reported a press");
    ok(!HomeDoublePress.arrived(t), "cancel fixture first press fired");
    ok(HomeDoublePress.clear(), "clear did not report the cancelled incomplete press");
    ok(!HomeDoublePress.arrived(t+1), "cancelled press survived clear");
    System.out.println("ALL OK");
  }
}
"""


@unittest.skipUnless(HAVE_JDK, "javac/java not installed")
class DoubleHomeRuns(unittest.TestCase):
    def test_single_double_batched_stale_and_backwards_time(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            pkg = d / "place/poster/app/home"
            pkg.mkdir(parents=True)
            shutil.copy(HOME / "HomeDoublePress.java", pkg / "HomeDoublePress.java")
            (d / "DoubleHarness.java").write_text(DOUBLE_HARNESS)
            c = subprocess.run(["javac", "-d", str(d), str(pkg / "HomeDoublePress.java"),
                                str(d / "DoubleHarness.java")], capture_output=True, text=True)
            self.assertEqual(c.returncode, 0, c.stderr)
            r = subprocess.run(["java", "-cp", str(d), "DoubleHarness"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_native_pair_routes_to_one_shot_feed_top_and_client_clears_scroll_memory(self):
        home = (HOME / "HomeActivity.java").read_text()
        phone = (ROOT / "static/js/client/phoneshell.js").read_text()
        app = (ROOT / "static/js/client/app.js").read_text()
        self.assertIn('openApp("__feed_top")', home)
        checks = (ROOT / "scripts/android_device_checks.sh").read_text()
        self.assertIn("double HOME took the native feed-top path", checks)
        self.assertIn("home double press: opening active feed at top", checks)
        self.assertIn("v === '__feed_top'", phone)
        self.assertIn("PC.timelineTop()", phone)
        self.assertNotIn("PC.timelineTop('global')", phone)

    def test_first_home_press_is_counted_when_android_only_resumes_the_activity(self):
        """Real launchers disagree about whether returning to an existing HOME activity sends
        onNewIntent.  A tracker wired only there sees the second physical press as press one and can
        never fire.  onStart is the lifecycle callback Android does guarantee for hidden->visible."""
        home = strip_comments((HOME / "HomeActivity.java").read_text())
        start = method(home, "protected void onStart")
        stop = method(home, "protected void onStop")
        new_intent = method(home, "protected void onNewIntent")
        create = method(home, "protected void onCreate")
        self.assertIn("main.postDelayed(countHomeStart, HOME_START_ECHO_MS)", start)
        self.assertIn("!homeIntentBeforeStart", start)
        self.assertIn("homeVisible = false", stop)
        self.assertIn("homeIntentBeforeStart = true", new_intent)
        self.assertIn("main.removeCallbacks(countHomeStart)", new_intent)
        self.assertIn("HomeDoublePress.arrived", method(home, "public void run"))
        self.assertNotIn("HomeDoublePress.arrived", create,
                         "onCreate plus onStart can turn one slow cold launch into a double press")

    def test_onstart_only_launcher_restore_returns_to_the_desktop(self):
        """OEMs that omit onNewIntent must not restore a stale drawer/edit overlay."""
        home = strip_comments((HOME / "HomeActivity.java").read_text())
        start = method(home, "protected void onStart")
        self.assertIn("closeDrawer()", start)
        self.assertIn("desk.clearEditing()", start)

    def test_one_home_cannot_be_counted_by_start_then_new_intent(self):
        """OEMs may order one HOME as onStart -> onNewIntent, opposite the original guard."""
        home = strip_comments((HOME / "HomeActivity.java").read_text())
        start = method(home, "protected void onStart")
        new_intent = method(home, "protected void onNewIntent")
        self.assertIn("homeStartPending = true", start)
        self.assertIn("if (homeStartPending)", new_intent)
        self.assertIn("homeStartPending = false", new_intent)
        self.assertIn("main.removeCallbacks(countHomeStart)", new_intent)
        self.assertIn("if (homeWindowFocused && !cancelledPairBeforeStart)", new_intent)
        self.assertIn("cancelledPairBeforeStart = false", new_intent)
        focus = method(home, "public void onWindowFocusChanged")
        self.assertIn("homeWindowFocused = hasFocus", focus)
        self.assertGreaterEqual(new_intent.count("HomeDoublePress.arrived"), 2,
                                "a focused fast second HOME must commit the pending first press")

    def test_feed_top_reloads_the_active_timeline_before_scrolling(self):
        app = (ROOT / "static/js/client/app.js").read_text()
        start = app.index("function timelineTop(view)")
        end = app.index("function setMobileNav", start)
        body = app[start:end]
        self.assertIn("Relay.reviveStale()", body)
        self.assertIn("else renderView(true)", body)
        self.assertLess(body.index("renderView(true)"), body.index("f.scrollTop=0"))
        self.assertIn("_TL_TABS.includes(VIEW) ? VIEW : _startTimeline()", app)
        self.assertIn("if(hidden.has(v))", app)
        self.assertIn("delete _tlScrollMemo[v]", app)
        self.assertIn("_tlForceTop=v", app)
        self.assertIn("if(VIEW === view && !forceTop)", app)
        self.assertIn("if(forceTop){", app)
        self.assertIn("_tlForceTop=''", app)

    def test_native_feed_top_waits_until_the_webview_has_resumed(self):
        """onNewIntent precedes Android's foreground WebView scroll restoration."""
        phone = (ROOT / "static/js/client/phoneshell.js").read_text()
        self.assertIn("_feedTopWaiting=true", phone)
        self.assertIn("consumeLaunchView().finally(settleFeedTop)", phone)
        self.assertIn("A.addListener('resume', again)", phone)
        self.assertIn("document.visibilityState === 'hidden'", phone)
        self.assertIn("setTimeout(settleFeedTop,700)", phone)
        top_call = phone.index("PC.timelineTop()")
        settle = phone.index("function settleFeedTop")
        landing = phone.index("if(v === '__feed_top')")
        self.assertGreater(top_call, settle)
        self.assertLess(top_call, landing,
                        "the landing must park the request, not scroll during onNewIntent")

    def test_native_duplicate_carriers_land_only_once(self):
        phone = (ROOT / "static/js/client/phoneshell.js").read_text()
        consume = method(strip_comments(phone), "function consumeLaunchView")
        init = method(strip_comments(phone), "function init")
        self.assertIn("_launchQueue.then(run, run)", consume)
        self.assertIn("direct || parked", consume)
        self.assertIn("v === _lastLaunchView", consume)
        launched = init[init.index("const launched"):]
        self.assertIn("consumeLaunchView(v)", launched)
        self.assertNotIn("landView(v);", launched,
                         "onNewIntent lands directly and then its parked copy lands a second time")

    def test_home_top_cancels_an_older_scroll_restore_and_holds_past_its_retry_window(self):
        app = (ROOT / "static/js/client/app.js").read_text()
        top = method(strip_comments(app), "function timelineTop")
        restore = method(strip_comments(app), "function _putScroll")
        self.assertIn("++_scrollRestoreGen", top)
        self.assertIn("mine !== _scrollRestoreGen", restore)
        self.assertIn("1600", top)
        self.assertIn("pointerdown", top)


if __name__ == "__main__":
    unittest.main()
