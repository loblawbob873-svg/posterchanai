"""THE DEVICE TESTS MUST COMPILE, and until now nothing checked that they did.

This is `tests/test_android_sync_compiles.py`'s lesson applied to the one place it had not been: a
test that cannot compile is a test that does not exist, only quieter — and these are the tests that
exist BECAUSE static checking cannot see a launcher, a role or a widget.

It has already happened. `LauncherDeviceTest` was missing a single import, so
`compileDebugAndroidTestJavaWithJavac` failed and every instrumented test on the device was skipped.
That was invisible from here for two reasons at once: nothing local compiled androidTest, and the
emulator workflow was itself broken in a way that meant `connectedDebugAndroidTest` had never run.
Two silent failures stacked, and the visible symptom was a red job with a green-looking log.

Compiled against the real android.jar (tests/androidcompile.py) plus small stubs for JUnit and
androidx.test — the frameworks are not on this box, and their absence must not be the reason the
check does not run.
"""
import glob
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

ANDROID_TEST = os.path.join(ac.ROOT, "mobile", "android", "app", "src", "androidTest", "java")


@unittest.skipIf(not shutil.which("javac"), "no javac on this node")
@unittest.skipIf(ac.android_jar() is None, "no android.jar on this node")
@unittest.skipIf(not os.path.isdir(ANDROID_TEST), "no androidTest sources here")
class DeviceTestsCompile(unittest.TestCase):

    def _sources(self):
        return sorted(glob.glob(os.path.join(ANDROID_TEST, "**", "*.java"), recursive=True))

    def test_every_instrumented_test_compiles(self):
        src = self._sources()
        self.assertTrue(src, "no androidTest sources — the path moved and this stopped checking")
        # The app's own sources come along, because these tests call into them and a signature that
        # moved is exactly the drift this is here to catch.
        app = []
        for pkg in ("home", "ui", "sms", "phone", "shortcut", "weather"):
            app += glob.glob(os.path.join(ac.JAVA, "place", "poster", "app", pkg, "*.java"))
        shims = {
            "place/poster/app/signer/SignerRelayService.java": """
package place.poster.app.signer;
public class SignerRelayService {
  public static void archiveIncoming(android.content.Context c, String f, String b, long w) { }
  public static void archiveDelete(android.content.Context c, String id) { }
}
""",
            "com/klinker/android/send_message/Settings.java": """
package com.klinker.android.send_message;
public class Settings {
  public void setUseSystemSending(boolean b) { }
  public void setSubscriptionId(int id) { }
}
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
  public Transaction setExplicitBroadcastForSentMms(android.content.Intent i) { return this; }
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
            # MusicService needs androidx.media, which is not on this box; the launcher only uses
            # these four members of it. NOT compile-checked here — CI's assembleDebug is.
            "place/poster/app/music/MusicService.java": """
package place.poster.app.music;
public class MusicService {
  public static final String ACTION_UPDATE = "update";
  public static final String ACTION_TOGGLE = "x";
  public static final String ACTION_STOP = "stop";
  public static final String EXTRA_TITLE = "title";
  public static final String EXTRA_ARTIST = "artist";
  public static final String EXTRA_PLAYING = "playing";
  public static final String EXTRA_POSITION = "position";
  public static final String EXTRA_DURATION = "duration";
  public interface Watcher { void onNowPlaying(String t, String a, boolean p); }
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
            "place/poster/app/MainActivity.java":
                "package place.poster.app;\npublic class MainActivity extends android.app.Activity { }\n",
        }
        with tempfile.TemporaryDirectory() as out:
            r = ac.compile_sources(sorted(set(src + app)), out, shims=shims)
        self.assertEqual(r.returncode, 0,
                         (r.stdout[-2000:] + "\n" + r.stderr[-6000:]).strip())

    def test_the_tests_that_matter_are_among_them(self):
        """Named, because these are the ones that can see what nothing here can: the launcher on a
        real home screen, the SMS role, and whether an icon actually paints pixels."""
        names = {os.path.basename(p) for p in self._sources()}
        for f in ("LauncherDeviceTest.java", "SmsDeviceTest.java", "DialerDeviceTest.java"):
            self.assertIn(f, names)


if __name__ == "__main__":
    unittest.main()
