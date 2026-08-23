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

APP = os.path.join(ac.JAVA, "place", "poster", "app")
HOME = os.path.join(APP, "home")
UI = os.path.join(APP, "ui")
SMS = os.path.join(APP, "sms")
PHONE = os.path.join(APP, "phone")
SHORTCUT = os.path.join(APP, "shortcut")

# The phone shell: the packages that are plain platform API and therefore compilable here.
SHELL = ["home", "ui", "sms", "phone", "shortcut", "weather"]
PACKAGES = [os.path.join(APP, p) for p in SHELL]

# EVERY OTHER PACKAGE UNDER place/poster/app, NAMED — not because this file compiles them, but so
# that a package which appears LATER cannot quietly have no compile coverage at all. The earlier
# version of this list claimed to be "discovered" and was in fact four typed paths; `shortcut` was
# added and compiled by nothing here until the claim was made true. They are excluded because each
# needs androidx, Capacitor or the media library, none of which is on this box; CI's assembleDebug
# is their compile check.
NOT_SHELL = {
    "calendar", "call", "contacts", "gamepad", "music", "nip55", "push", "scan",
    "screenshare", "share", "signer", "sync", "tor", "vault",
}

# WHAT IS SHIMMED, AND WHY. HomeActivity reads the music service's now-playing state and presses its
# widget's button. That service is built on androidx.media (MediaSessionCompat, MediaButtonReceiver),
# which is not on this box — so it is replaced here by the four members the launcher actually uses.
# MusicService itself is therefore NOT compile-checked by this file; CI's assembleDebug is.
SHIMS = {
    # The shell calls into the native signer only to archive a delivered SMS. The real service uses
    # OkHttp/AndroidX and is compile-checked by Gradle plus test_android_signer_service.py.
    "place/poster/app/signer/SignerRelayService.java": """
package place.poster.app.signer;
public class SignerRelayService {
  public static void archiveIncoming(android.content.Context c, String f, String b, long w) { }
}
""",
    # MMS transport is an external Android library present in Gradle, not android.jar. Keep these
    # signatures narrow so this test still checks every call the SMS UI makes into it.
    "com/klinker/android/send_message/Settings.java": """
package com.klinker.android.send_message;
public class Settings { public void setUseSystemSending(boolean b) { } }
""",
    "com/klinker/android/send_message/Message.java": """
package com.klinker.android.send_message;
public class Message {
  public Message(String b, String a, byte[] image) { }
  public Message(String b, String a, android.graphics.Bitmap image) { }
  public void setSave(boolean save) { }
}
""",
    "com/klinker/android/send_message/Transaction.java": """
package com.klinker.android.send_message;
public class Transaction {
  public static final long NO_THREAD_ID = -1;
  public Transaction(android.content.Context c, Settings s) { }
  public void sendNewMessage(Message m) { }
}
""",
    "com/android/mms/transaction/PushReceiver.java": """
package com.android.mms.transaction;
public class PushReceiver extends android.content.BroadcastReceiver {
  @Override public void onReceive(android.content.Context c, android.content.Intent i) { }
}
""",
    "com/klinker/android/send_message/MmsReceivedReceiver.java": """
package com.klinker.android.send_message;
public abstract class MmsReceivedReceiver extends android.content.BroadcastReceiver {
  public abstract void onMessageReceived(android.content.Context c, android.net.Uri u);
  public abstract void onError(android.content.Context c, String e);
  @Override public void onReceive(android.content.Context c, android.content.Intent i) { }
}
""",
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
        self.assertTrue(os.path.isdir(SHORTCUT))

    def test_a_new_package_is_classified_rather_than_forgotten(self):
        """The floor's real guarantee. A package added under place/poster/app must be either part of
        the shell (and compiled above) or deliberately listed as not — never simply absent, which is
        how `shortcut` would have shipped with no compile check on this box at all."""
        found = set(n for n in os.listdir(APP) if os.path.isdir(os.path.join(APP, n)))
        unclassified = found - set(SHELL) - NOT_SHELL
        self.assertEqual(set(), unclassified,
                         "new package(s) under place/poster/app: add to SHELL (and it gets "
                         "compiled here) or to NOT_SHELL (and say why): " + ", ".join(sorted(unclassified)))

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
