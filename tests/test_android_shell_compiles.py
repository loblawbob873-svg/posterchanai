"""THE PHONE SHELL COMPILES — against the real Android SDK, on a box with no Gradle.

`tests/test_android_sync_compiles.py` is this file's ancestor and states the reason: a test that
cannot compile is a test that does not exist, only quieter, and the Android half of this app is
invisible on this machine because the Gradle daemon will not stay up on it.

The difference here is the SDK. That test compiles against tests/androidstubs, a hand-written
skeleton of the platform; this one compiles against the genuine `android.jar` (see
tests/androidcompile.py), because the launcher, the messages app and the dialer are almost ENTIRELY
platform API — `queryIntentActivities`, `setComponentEnabledSetting`, `RoleManager`,
`TelecomManager`, `Telephony.Sms`, `SmsManager`, `InCallService` — and a stub of the very thing under
test proves nothing about it.

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
SMS = os.path.join(ac.JAVA, "place", "poster", "app", "sms")
PHONE = os.path.join(ac.JAVA, "place", "poster", "app", "phone")

# THE FLOOR IS DISCOVERED, NOT TYPED. A package added under place/poster/app that belongs to the
# phone shell joins this compile the moment it exists — the alternative is a new package with no
# compile coverage at all, which is the failure `tests/test_android_sync_compiles.py` was written
# after living through.
PACKAGES = [HOME, UI, SMS, PHONE]

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
class ShellCompiles(unittest.TestCase):

    def test_the_whole_phone_shell_compiles(self):
        src = []
        for pkg in PACKAGES:
            src += glob.glob(os.path.join(pkg, "*.java"))
        src = sorted(src)
        self.assertTrue(src, "no sources found — the path moved and this test stopped checking")
        with tempfile.TemporaryDirectory() as out:
            r = ac.compile_sources(src, out, shims=SHIMS)
        # BOTH streams, and the count. javac puts "N errors" on stdout and the errors themselves on
        # stderr, so a message carrying only one of them can leave a failure looking like it had no
        # cause at all.
        self.assertEqual(r.returncode, 0,
                         (r.stdout[-2000:] + "\n" + r.stderr[-6000:]).strip())

    def test_the_packages_it_claims_to_cover_are_actually_there(self):
        """A path that moved turns this whole file into a test of an empty list, which passes."""
        self.assertTrue(os.path.isdir(HOME))
        self.assertTrue(os.path.isdir(UI))
        self.assertTrue(os.path.isdir(SMS))

    def test_the_home_activity_is_among_them(self):
        """Named, because it is the file this test exists for: the one that draws the phone's home
        screen, and the one whose failure has no fallback."""
        self.assertTrue(os.path.exists(os.path.join(HOME, "HomeActivity.java")))

    def test_every_resource_the_shell_names_actually_exists(self):
        """R is synthesised from the real res/ tree, so a `R.id.pc_home_grid` that no layout declares
        fails the compile above rather than reaching a phone as a null view and a blank home screen."""
        with tempfile.TemporaryDirectory() as out:
            path = ac.write_r(out)
            body = open(path).read()
        for name in ("pc_home_grid", "pc_home_root", "pc_cell_icon", "home_activity",
                     "home_cell", "PcHomeTheme", "ic_pc_gear",
                     "pc_sms_threads", "pc_th_input", "sms_bubble", "PcAppTheme", "sms_title"):
            self.assertIn(name, body, name + " is not a real resource")


if __name__ == "__main__":
    unittest.main()
