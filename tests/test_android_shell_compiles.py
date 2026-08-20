"""THE LAUNCHER COMPILES — against the real Android SDK, on a box with no Gradle.

`tests/test_android_sync_compiles.py` is this file's ancestor and states the reason: a test that
cannot compile is a test that does not exist, only quieter, and the Android half of this app is
invisible on this machine because the Gradle daemon will not stay up on it.

The difference here is the SDK. That test compiles against tests/androidstubs, a hand-written
skeleton of the platform; this one compiles against the genuine `android.jar` (see
tests/androidcompile.py), because a launcher is almost entirely platform API — `queryIntentActivities`,
`setComponentEnabledSetting`, `RoleManager`, `TelecomManager`, `Telephony` — and a stub of the very
thing under test proves nothing about it.

It is a FLOOR, not a ceiling: `tests/test_android_launcher.py` runs the decisions,
`mobile/android/app/src/androidTest` runs them on a real device, and CI's `assembleDebug` is the last
word. This is what fails in seconds when a platform call is wrong.
"""
import glob
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

HOME = os.path.join(ac.JAVA, "place", "poster", "app", "home")
UI = os.path.join(ac.JAVA, "place", "poster", "app", "ui")

# WHAT IS SHIMMED, AND WHY. HomeActivity reads the music service's now-playing state and presses its
# widget's button. That service is built on androidx.media (MediaSessionCompat, MediaButtonReceiver),
# which is not on this box — so it is replaced here by the four members the launcher actually uses.
# MusicService itself is therefore NOT compile-checked by this file; CI's assembleDebug is.
SHIMS = {
    "place/poster/app/music/MusicService.java": """
package place.poster.app.music;
public class MusicService {
  public static final String ACTION_TOGGLE = "place.poster.app.MUSIC_TOGGLE";
  public interface Watcher { void onNowPlaying(String title, String artist, boolean playing); }
  public static void setWatcher(Watcher w) { }
  public static String nowTitle() { return ""; }
  public static String nowArtist() { return ""; }
  public static boolean nowPlaying() { return false; }
}
""",
    "place/poster/app/music/MusicWidget.java": """
package place.poster.app.music;
public class MusicWidget extends android.content.BroadcastReceiver {
  @Override public void onReceive(android.content.Context c, android.content.Intent i) { }
}
""",
    "place/poster/app/MainActivity.java": """
package place.poster.app;
public class MainActivity extends android.app.Activity { }
""",
}


@unittest.skipIf(not shutil.which("javac"), "no javac on this node")
@unittest.skipIf(ac.android_jar() is None, "no android.jar on this node")
@unittest.skipIf(not os.path.isdir(HOME), "no android sources here")
class HomeCompiles(unittest.TestCase):

    def test_the_whole_launcher_package_compiles(self):
        src = sorted(glob.glob(os.path.join(HOME, "*.java")) + glob.glob(os.path.join(UI, "*.java")))
        self.assertTrue(src, "no sources found — the path moved and this test stopped checking")
        with tempfile.TemporaryDirectory() as out:
            r = ac.compile_sources(src, out, shims=SHIMS)
        self.assertEqual(r.returncode, 0, r.stderr[-6000:])

    def test_the_home_activity_is_among_them(self):
        """Named, because it is the file this test exists for: the one that draws the phone's home
        screen, and the one whose failure has no fallback."""
        self.assertTrue(os.path.exists(os.path.join(HOME, "HomeActivity.java")))

    def test_every_resource_the_launcher_names_actually_exists(self):
        """R is synthesised from the real res/ tree, so a `R.id.pc_home_grid` that no layout declares
        fails the compile above rather than reaching a phone as a null view and a blank home screen."""
        with tempfile.TemporaryDirectory() as out:
            path = ac.write_r(out)
            body = open(path).read()
        for name in ("pc_home_grid", "pc_home_root", "pc_cell_icon", "home_activity",
                     "home_cell", "PcHomeTheme", "ic_pc_gear"):
            self.assertIn(name, body, name + " is not a real resource")


if __name__ == "__main__":
    unittest.main()
