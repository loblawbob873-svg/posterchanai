"""Being the launcher must not turn folder sync loose on a phone somebody is holding.

    "since we will be the launcher on phone, you really need to make sure that folder sync don't
     fuck up and restart everything"

The first half of the answer is that it cannot, and that is worth pinning rather than asserting in
prose: a sweep starts only when a folder is DUE by its own interval, and the two engines are kept off
one folder by the claim set. Pressing HOME re-scans nothing and re-uploads nothing. There is no
mechanism by which becoming the home app restarts a sync.

The second half is a genuine regression, and it is about WHEN "the app is backgrounded" happens.
MainActivity pausing used to mean the person had left PosterChan — a few times a day, usually with
the screen about to go off. As the home app it means they pressed HOME, which is the resting state of
the phone and happens dozens of times an hour, every one of them with the screen on. A sweep started
there takes the folder's claim moments before they open the app, and a page refused its claim can
only say "syncing in the background" — which the sync code itself calls a hang the user caused by
opening the app. As a launcher that stops being a coincidence and becomes the ordinary way the app
gets opened.

So a due sweep stands down while our home screen is up and the screen is interactive. Both halves are
checked, plus the two ways the rule could be worse than the bug: never syncing at all, and deferring
when the screen is OFF (the best possible time to sync).
"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_android_launch_view import method, strip_comments

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "mobile/android/app/src/main/java/place/poster/app"
HOME = JAVA / "home"
SYNC = JAVA / "sync"

HAVE_JDK = shutil.which("javac") and shutil.which("java")

HARNESS = r"""
import place.poster.app.home.LauncherState;

public class Harness {
    static int failed = 0;
    static void ok(String what, boolean cond) {
        if (!cond) { failed++; System.out.println("FAIL " + what); }
        else System.out.println("ok   " + what);
    }

    public static void main(String[] a) {
        long t = 1_000_000_000L;

        // Nobody at the home screen: the sweep is none of this rule's business.
        LauncherState.homeHidden();
        ok("in an app, screen on: sweep", !LauncherState.deferSweep(true, t));
        ok("in an app, screen off: sweep", !LauncherState.deferSweep(false, t));

        // At the home screen with the screen ON is the case the rule exists for: they are one tap
        // from opening something, quite possibly this app.
        LauncherState.homeShown(t);
        ok("at home, screen on: defer", LauncherState.deferSweep(true, t + 1));

        // SCREEN OFF AT THE HOME SCREEN IS THE BEST TIME TO SYNC — a phone in a pocket showing the
        // home screen is the ordinary idle state, and deferring there would mean never syncing.
        ok("at home, screen off: sweep", !LauncherState.deferSweep(false, t + 1));

        // It cannot starve. A phone parked awake on its home screen must be LATE, not broken.
        ok("just inside the bound still defers",
           LauncherState.deferSweep(true, t + LauncherState.MAX_DEFER_MS - 1));
        ok("past the bound it gives way",
           !LauncherState.deferSweep(true, t + LauncherState.MAX_DEFER_MS));
        ok("and stays given way",
           !LauncherState.deferSweep(true, t + LauncherState.MAX_DEFER_MS * 4));

        // A redraw, a rotation or the drawer opening must not restart the clock, or the bound above
        // could never be reached and the deferral would be unbounded after all.
        LauncherState.homeShown(t);
        LauncherState.homeShown(t + LauncherState.MAX_DEFER_MS - 5);
        ok("re-showing does not restart the clock",
           !LauncherState.deferSweep(true, t + LauncherState.MAX_DEFER_MS));

        // Leaving and coming back DOES, because that is a new stretch at the home screen.
        LauncherState.homeHidden();
        LauncherState.homeShown(t + LauncherState.MAX_DEFER_MS);
        ok("leaving and returning starts a new stretch",
           LauncherState.deferSweep(true, t + LauncherState.MAX_DEFER_MS + 1));

        // A clock that moved backwards must not latch a deferral that never expires.
        LauncherState.homeShown(t);
        ok("a backwards clock does not latch", !LauncherState.deferSweep(true, t - 1000));

        // Opening an app clears it immediately — the page is the better engine and should get it.
        LauncherState.homeShown(t);
        LauncherState.homeHidden();
        ok("opening something clears the deferral", !LauncherState.deferSweep(true, t + 1));
        ok("atHome() agrees", !LauncherState.atHome());

        // "Are we the home screen" — proven by our home screen having RUN, not by a lookup.
        LauncherState.resetForTest();
        ok("a fresh process is not assumed to be the launcher", !LauncherState.weAreTheHomeScreen());
        ok("...so a pause with the screen on sweeps as before",
           !LauncherState.deferHandover(true));
        LauncherState.homeShown(t);
        LauncherState.homeHidden();
        ok("having shown the home screen once is enough", LauncherState.weAreTheHomeScreen());

        // The handover rule knows LESS than deferSweep, because MainActivity.onPause runs before
        // HomeActivity.onStart and `showing` is still false at that instant.
        ok("pause with the screen on, we are the launcher: hold",
           LauncherState.deferHandover(true));
        ok("pause with the screen OFF: sweep, exactly as before",
           !LauncherState.deferHandover(false));

        System.out.println(failed == 0 ? "ALL OK" : (failed + " FAILED"));
        if (failed != 0) System.exit(1);
    }
}
"""


@unittest.skipUnless(HAVE_JDK, "javac/java not installed")
class TheQuietWindowRuns(unittest.TestCase):
    def test_the_rule_behaves(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            pkg = d / "place/poster/app/home"
            pkg.mkdir(parents=True)
            shutil.copy(HOME / "LauncherState.java", pkg / "LauncherState.java")
            (d / "Harness.java").write_text(HARNESS)
            c = subprocess.run(["javac", "-d", str(d), str(pkg / "LauncherState.java"),
                                str(d / "Harness.java")], capture_output=True, text=True, cwd=d)
            self.assertEqual(c.returncode, 0, c.stderr)
            r = subprocess.run(["java", "-cp", str(d), "Harness"],
                               capture_output=True, text=True, cwd=d)
            self.assertIn("ALL OK", r.stdout, r.stdout + r.stderr)


class TheHomeScreenReportsItself(unittest.TestCase):
    """LauncherState is only as true as the two calls that maintain it."""

    def test_home_start_and_stop_are_wired(self):
        src = (HOME / "HomeActivity.java").read_text()
        self.assertIn("LauncherState.homeShown(", src)
        self.assertIn("LauncherState.homeHidden()", src)

    def test_shown_is_in_onstart_and_hidden_in_onstop(self):
        src = strip_comments((HOME / "HomeActivity.java").read_text())
        self.assertIn("LauncherState.homeShown(", method(src, "protected void onStart"))
        self.assertIn("LauncherState.homeHidden()", method(src, "protected void onStop"))


class TheSweepConsultsIt(unittest.TestCase):
    def test_the_plan_stands_down_at_the_home_screen(self):
        src = strip_comments((SYNC / "NativeRunner.java").read_text())
        self.assertIn("LauncherState.deferSweep(", method(src, "static Plan plan"))

    def test_it_is_asked_after_the_cheap_checks(self):
        """The deferral reads PowerManager and the package manager. A folder that is not due, or an
        account with no key on this device, must still be answered from a prefs read."""
        body = method(strip_comments((SYNC / "NativeRunner.java").read_text()), "static Plan plan")
        self.assertLess(body.index("wrappedDriveKey"), body.index("LauncherState.deferSweep("))

    def test_the_pause_handoff_holds_when_we_are_the_home_app(self):
        """MainActivity.onPause runs BEFORE HomeActivity.onStart, so LauncherState cannot answer at
        that moment — the screen being on plus holding the home role is what stands in for it."""
        src = strip_comments((SYNC / "FolderSyncPlugin.java").read_text())
        body = method(src, "private void handOver")
        self.assertIn("leavingIntoOurOwnLauncher(ctx)", body)
        check = method(src, "private static boolean leavingIntoOurOwnLauncher")
        self.assertIn("isInteractive()", check)
        self.assertIn("LauncherState.deferHandover(", check)

    def test_sync_does_not_reach_into_the_launcher_package(self):
        """"Are we the home screen" is answered by LauncherState rather than HomeRoles on purpose.
        Importing the launcher from `sync` drags HomeRoles, HomeActivity, DeskView and MainActivity
        into test_android_sync_compiles' javac over `sync` alone — measured, thirty-three errors on
        that one import. A dependency that costs the compile floor is the wrong dependency."""
        for f in sorted(SYNC.glob("*.java")):
            with self.subTest(file=f.name):
                src = strip_comments(f.read_text())
                for reach in ("HomeRoles", "HomeActivity", "DeskView", "AppRepo"):
                    self.assertNotIn(reach, src,
                                     "%s reaches into the launcher for %s" % (f.name, reach))

    def test_an_inflight_page_is_always_asked_to_checkpoint(self):
        """Launcher deferral must not skip the checkpoint request, but ownership remains with the
        page until its release acknowledgement so a native writer cannot overlap it."""
        body = method(strip_comments((SYNC / "FolderSyncPlugin.java").read_text()),
                      "private void handOver")
        self.assertLess(body.index('notifyListeners("folderSyncHandoff"'),
                        body.index("leavingIntoOurOwnLauncher(ctx)"),
                        "launcher deferral skips asking the page for its durable checkpoint")
        self.assertNotIn("releaseAll", body,
                         "onPause steals ownership before the page has checkpointed")


class NothingAboutBeingTheLauncherReSyncs(unittest.TestCase):
    """The user's actual fear. A sweep starts only when a folder is due by its own interval."""

    def test_a_sweep_still_requires_a_due_folder(self):
        body = method(strip_comments((SYNC / "NativeRunner.java").read_text()), "static Plan plan")
        self.assertIn("SyncDiff.shouldSync(", body,
                      "plan() no longer asks whether a folder is due — every trigger would sweep")

    def test_a_folder_another_engine_holds_is_never_swept(self):
        body = method(strip_comments((SYNC / "NativeRunner.java").read_text()), "static Plan plan")
        self.assertIn("NativeSweep.claimed(", body)


if __name__ == "__main__":
    unittest.main()
