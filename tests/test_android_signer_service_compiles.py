"""SignerRelayService COMPILES, against the real android.jar.

Everything else that checks this file reads it as text. That is the right tool for "is the ping
interval still four minutes" and the wrong one for "does this build": the service is the phone's
whole signer AND — since the launcher's Texts app started backing up its own history — the thing
that publishes the SMS archive. Its only compile coverage was CI's `assembleDebug`, which runs
after a push, so the way to find a typo in it was to ship one.

Java ignores return types when comparing signatures, which is how a duplicate method once stopped
the music module compiling while every regex test here stayed green (see the Music player notes in
CLAUDE.md). This is the same lesson applied to the file next door.

okhttp is not on this box, so it is shimmed down to the handful of members the service actually
uses — narrow on purpose, so a call this service makes that okhttp does not offer still fails here.
The shim cannot prove the real library agrees; Gradle does that. What it proves is that the code
around it is Java.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java")

import test_android_shell_compiles as shell  # noqa: E402

# ONE COPY OF THE SHIM SET. The phone-shell compile already shims the MMS transport library,
# the music service and MainActivity for exactly the same reason — they are not on this box.
# Its SignerRelayService shim is dropped here, because compiling the REAL one is the point.
SHIMS = dict(shell.SHIMS)
SHIMS.pop("place/poster/app/signer/SignerRelayService.java", None)
SHIMS.update({
    "okhttp3/OkHttpClient.java": """
package okhttp3;
public class OkHttpClient {
  public static class Builder {
    public Builder pingInterval(long v, java.util.concurrent.TimeUnit u) { return this; }
    public Builder retryOnConnectionFailure(boolean b) { return this; }
    public Builder connectTimeout(long v, java.util.concurrent.TimeUnit u) { return this; }
    public Builder readTimeout(long v, java.util.concurrent.TimeUnit u) { return this; }
    public Builder writeTimeout(long v, java.util.concurrent.TimeUnit u) { return this; }
    public Builder callTimeout(long v, java.util.concurrent.TimeUnit u) { return this; }
    public OkHttpClient build() { return new OkHttpClient(); }
  }
  public WebSocket newWebSocket(Request r, WebSocketListener l) { return null; }
  public java.util.concurrent.ExecutorService dispatcherExecutor() { return null; }
  public Dispatcher dispatcher() { return new Dispatcher(); }
  public ConnectionPool connectionPool() { return new ConnectionPool(); }
}
""",
    "okhttp3/Dispatcher.java": """
package okhttp3; public class Dispatcher { public void cancelAll() { } }
""",
    "okhttp3/ConnectionPool.java": """
package okhttp3; public class ConnectionPool { public void evictAll() { } }
""",
    "okhttp3/Request.java": """
package okhttp3;
public class Request {
  public static class Builder {
    public Builder url(String u) { return this; }
    public Builder header(String k, String v) { return this; }
    public Request build() { return new Request(); }
  }
}
""",
    "okhttp3/Response.java": """
package okhttp3;
public class Response {
  public int code() { return 0; }
  public String message() { return ""; }
}
""",
    "okhttp3/WebSocket.java": """
package okhttp3;
public interface WebSocket {
  boolean send(String text);
  boolean close(int code, String reason);
  void cancel();
}
""",
    "okhttp3/WebSocketListener.java": """
package okhttp3;
public abstract class WebSocketListener {
  public void onOpen(WebSocket s, Response r) { }
  public void onMessage(WebSocket s, String text) { }
  public void onClosing(WebSocket s, int code, String reason) { }
  public void onClosed(WebSocket s, int code, String reason) { }
  public void onFailure(WebSocket s, Throwable t, Response r) { }
}
""",
    "place/poster/app/push/StayAwakeService.java": """
package place.poster.app.push;
public class StayAwakeService extends android.app.Service {
  public static boolean running = false;
  public static final String ACTION_STOP = "stop";
  public static boolean wanted(android.content.Context c) { return false; }
  public android.os.IBinder onBind(android.content.Intent i) { return null; }
}
""",
    "place/poster/app/push/PushEventService.java": """
package place.poster.app.push;
public class PushEventService {
  public static final String PREFS = "pcai_push";
  public static boolean running = false;
  public static boolean deliver(android.content.Context c, String payload) { return true; }
}
""",
    "place/poster/app/music/MusicPlugin.java": """
package place.poster.app.music;
public class MusicPlugin { }
""",
})

# The signer and the two packages it drives. `sms` is here because the service now publishes the
# launcher Texts app's back-fill (SmsArchive/SmsSweep); `sync` because that back-fill seals its
# attachments with the folder-sync drive key.
PACKAGES = ("signer", "sms", "sync")


@unittest.skipIf(not os.path.isdir(JAVA), "no android sources here")
class SignerCompiles(unittest.TestCase):
    def test_the_signer_service_and_the_sms_archive_compile(self):
        """The service, the sweep it drives, and the sync pieces both of them reach into."""
        jar = ac.android_jar()
        if not jar:
            self.skipTest("no android.jar on this machine")
        # EVERY FILE IN THOSE PACKAGES EXCEPT THE CAPACITOR PLUGINS. A *Plugin is the WebView's
        # door into a package: it needs Capacitor, which is not on this box, and it reaches sideways
        # into whatever the JavaScript happens to ask about (FolderSyncPlugin reports the stay-awake
        # service's state, which drags in androidx.media). None of them is on the path this file
        # exists to cover — the launcher screen calls the service directly, with no WebView at all.
        src = []
        for p in PACKAGES:
            for root, _dirs, files in os.walk(os.path.join(JAVA, "place", "poster", "app", p)):
                src += [os.path.join(root, f) for f in files
                        if f.endswith(".java") and not f.endswith("Plugin.java")]
        self.assertTrue(src, "no sources found — the path moved and this test stopped checking")
        with tempfile.TemporaryDirectory() as out:
            r = ac.compile_sources(src, out, shims=SHIMS)
        # BOTH streams: javac puts "N errors" on stdout and the errors themselves on stderr, so a
        # message carrying one of them can leave a failure looking like it had no cause at all.
        self.assertEqual(r.returncode, 0,
                         (r.stdout[-2000:] + "\n" + r.stderr[-6000:]).strip())
