"""The music widget performs the press it was given, and lands on the player.

    "on tablet: clicking play on music widget opens up default posterchan app page instead of music"

This is the SAME bug as tests/test_android_launch_view.py, in a second place, and it survived that
fix because nobody looked here. `MusicWidget.launch` and `MusicService.revive` both built their
intent with ACTION_MAIN plus CATEGORY_LAUNCHER at an activity declared singleTask — the exact intent
the home screen sends, whose contract is "bring this app back the way I left it", not "here is a
payload". On a WARM start the press went nowhere: the app animated forward on the last screen, the
music did not start, nothing threw and nothing logged.

`MainActivity.onNewIntent` calling `setIntent` is what made this look covered. It only runs when the
intent is DELIVERED, and a launcher-shaped intent at a singleTask activity is not.

Three halves, because any one of them passes while the bug is present:
  * LaunchPress is RUN (javac + java), including the staleness and consume-once rules;
  * neither music launch path dresses its intent as a launcher press;
  * a revive lands on the PLAYER, which is the other half of "opens the default page".
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_android_launch_view import method, strip_comments

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "mobile/android/app/src/main/java/place/poster/app"
MUSIC = JAVA / "music"

HAVE_JDK = shutil.which("javac") and shutil.which("java")

HARNESS = r"""
import place.poster.app.music.LaunchPress;

public class Harness {
    static int failed = 0;
    static void ok(String what, boolean cond) {
        if (!cond) { failed++; System.out.println("FAIL " + what); }
        else System.out.println("ok   " + what);
    }

    public static void main(String[] a) {
        long t = 1_000_000_000L;

        LaunchPress.request("play", t);
        ok("a parked press is returned", "play".equals(LaunchPress.take(t + 5)));

        // Only once. An unconsumed press is performed again on every later resume — a widget tap
        // that restarts the music hours afterwards, over somebody who had paused it.
        LaunchPress.request("play", t);
        LaunchPress.take(t + 5);
        ok("reading consumes it", "".equals(LaunchPress.take(t + 6)));

        // A minute-old press is somebody who has moved on.
        LaunchPress.request("next", t);
        ok("a stale press is dropped", "".equals(LaunchPress.take(t + LaunchPress.MAX_AGE_MS)));

        // A clock that went backwards must not make a fresh press look like it came from the future
        // and get performed for ever after.
        LaunchPress.request("play", t);
        ok("a backwards clock drops it", "".equals(LaunchPress.take(t - 1)));

        LaunchPress.request("  prev  ", t);
        ok("it is trimmed", "prev".equals(LaunchPress.take(t + 1)));

        LaunchPress.request("", t);
        ok("an empty press parks nothing", "".equals(LaunchPress.take(t + 1)));

        LaunchPress.request("play", t);
        ok("waiting sees it", LaunchPress.waiting(t + 1));
        LaunchPress.clear();
        ok("clear drops it", !LaunchPress.waiting(t + 1) && "".equals(LaunchPress.take(t + 1)));

        System.out.println(failed == 0 ? "ALL OK" : ("FAILED " + failed));
    }
}
"""


@unittest.skipIf(not HAVE_JDK, "no JDK on this node")
class LaunchPressRuns(unittest.TestCase):
    def test_the_handoff_behaves(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "Harness.java").write_text(HARNESS)
            r = subprocess.run(["javac", "-nowarn", "-d", str(d),
                                "-sourcepath", str(ROOT / "mobile/android/app/src/main/java"),
                                str(MUSIC / "LaunchPress.java"), str(d / "Harness.java")],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            r = subprocess.run(["java", "-cp", str(d), "Harness"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("ALL OK", r.stdout, r.stdout)


class TheMusicLaunchIsNotDressedAsALauncherPress(unittest.TestCase):
    """The regression itself. LaunchPress can be perfect and the bug still present."""

    PATHS = [(MUSIC / "MusicWidget.java", "private static Intent launch"),
             (MUSIC / "MusicService.java", "private void revive")]

    def _block(self, path, fn):
        return method(strip_comments(path.read_text()), fn)

    def test_neither_path_sends_a_home_screen_intent(self):
        for p, fn in self.PATHS:
            with self.subTest(file=p.name):
                block = self._block(p, fn)
                self.assertNotIn("CATEGORY_LAUNCHER", block,
                                 "%s dresses its intent as a home-screen press — a singleTask "
                                 "activity handed that intent is resumed as-was and the extras "
                                 "are discarded, so the press is never seen" % p.name)
                self.assertNotIn("ACTION_MAIN", block, p.name)

    def test_the_press_is_parked_as_well_as_sent(self):
        """The widget's PendingIntent is built once and reused, so its extra even carries the time
        the widget was DRAWN rather than tapped. The park is what makes a warm start work at all."""
        block = self._block(MUSIC / "MusicService.java", "private void revive")
        self.assertIn("LaunchPress.request(", block)
        self.assertLess(block.index("LaunchPress.request("), block.index("startActivity("),
                        "the press is parked after the app is started")

    def test_the_extra_is_kept_too(self):
        """A COLD start has no process to have parked anything, and is exactly the case where the
        extra always worked."""
        for p, fn in self.PATHS:
            with self.subTest(file=p.name):
                self.assertIn("EXTRA_LAUNCH_ACTION", self._block(p, fn), p.name)

    def test_a_revive_lands_on_the_player(self):
        """The other half of the report. Dragging the app to the foreground to perform a music press
        and putting the person on whatever screen they left is not answering the press."""
        block = self._block(MUSIC / "MusicService.java", "private void revive")
        self.assertIn("LaunchView.request(", block)
        self.assertIn("__music", block)


class ThePluginReadsBoth(unittest.TestCase):
    def setUp(self):
        self.body = method(strip_comments((MUSIC / "MusicPlugin.java").read_text()),
                           "public void consumeLaunchAction")

    def test_it_prefers_the_parked_press(self):
        self.assertIn("LaunchPress.take(", self.body)
        self.assertLess(self.body.index("LaunchPress.take("), self.body.index("getIntent()"),
                        "the intent extra is read first, so a warm start's stale extra wins over "
                        "the press that actually happened")

    def test_it_still_falls_back_to_the_extra(self):
        self.assertIn("EXTRA_LAUNCH_ACTION", self.body)

    def test_it_clears_the_extra_whichever_answered(self):
        self.assertIn("removeExtra", self.body)


class TheClientKnowsWhereMusicLives(unittest.TestCase):
    """`__music` is not a view slug — `switchView('__music')` falls through to the default screen,
    which is precisely what the report described. app.js's own More menu already spells it this way
    and opens it with `openMusic()`; one name, used by both."""

    def test_the_landing_opens_the_player(self):
        src = (ROOT / "static/js/client/phoneshell.js").read_text()
        self.assertIn("__music", src)
        self.assertIn("openMusic", src)

    def test_app_js_still_uses_the_same_name(self):
        self.assertIn("'__music'", (ROOT / "static/js/client/app.js").read_text())


class TheLandingIsNotDrivenByVisibilityAlone(unittest.TestCase):
    """`visibilitychange` on Android arrives late or is coalesced away — measured, and the same
    lesson that cost the timeline a release. The Activity's own resume and the native push are the
    two signals Android cannot swallow, and consuming is idempotent so all three may fire."""

    def test_three_triggers(self):
        src = (ROOT / "static/js/client/phoneshell.js").read_text()
        self.assertIn("visibilitychange", src)
        self.assertIn("'resume'", src)
        self.assertIn("'launchView'", src)

    def test_the_native_side_pushes_it(self):
        home = strip_comments((JAVA / "home/HomePlugin.java").read_text())
        self.assertIn("announceLaunchView", home)
        self.assertIn('notifyListeners("launchView"', home)
        main = strip_comments((JAVA / "MainActivity.java").read_text())
        self.assertIn("announceLaunchView", method(main, "public void onNewIntent"))
