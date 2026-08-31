"""Compile the first-party push transport against the real Android SDK without Gradle."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import androidcompile as ac  # noqa: E402


JAVA = os.path.join(ac.JAVA, "place", "poster", "app")
SOURCES = [
    os.path.join(JAVA, "push", name) for name in (
        "DirectPushStore.java", "DirectPushService.java", "PushEventService.java", "PushPlugin.java"
    )
]

SHIMS = {
    "okhttp3/OkHttpClient.java": """
package okhttp3;
public class OkHttpClient {
  public static class Builder {
    public Builder pingInterval(long v, java.util.concurrent.TimeUnit u) { return this; }
    public Builder connectTimeout(long v, java.util.concurrent.TimeUnit u) { return this; }
    public Builder readTimeout(long v, java.util.concurrent.TimeUnit u) { return this; }
    public Builder retryOnConnectionFailure(boolean b) { return this; }
    public OkHttpClient build() { return new OkHttpClient(); }
  }
  public WebSocket newWebSocket(Request r, WebSocketListener l) { return null; }
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
public class Request { public static class Builder {
  public Builder url(String u) { return this; } public Request build() { return new Request(); }
} }
""",
    "okhttp3/Response.java": "package okhttp3; public class Response { }",
    "okhttp3/WebSocket.java": """
package okhttp3; public interface WebSocket {
  boolean send(String s); boolean close(int code, String reason);
}
""",
    "okhttp3/WebSocketListener.java": """
package okhttp3; public abstract class WebSocketListener {
  public void onOpen(WebSocket s, Response r) { }
  public void onMessage(WebSocket s, String text) { }
  public void onClosing(WebSocket s, int code, String reason) { }
  public void onClosed(WebSocket s, int code, String reason) { }
  public void onFailure(WebSocket s, Throwable t, Response r) { }
}
""",
    "place/poster/app/RunningNote.java": """
package place.poster.app;
public final class RunningNote {
  public static final int ID=1, DIRECT=4;
  public static void ensureChannel(android.content.Context c) { }
  public static android.app.Notification build(android.content.Context c) { return null; }
  public static void refresh(android.content.Context c) { }
  public static boolean othersRunning(int me) { return false; }
}
""",
    "place/poster/app/MainActivity.java": """
package place.poster.app; public class MainActivity extends android.app.Activity { }
""",
    "place/poster/app/home/HomeActivity.java": """
package place.poster.app.home; public class HomeActivity {
  public static final String EXTRA_VIEW="view", EXTRA_VIEW_AT="at";
}
""",
    "place/poster/app/push/StayAwakeService.java": """
package place.poster.app.push; public class StayAwakeService extends android.app.Service {
  public static final String ACTION_START="start", ACTION_STOP="stop";
  public static boolean running=false;
  public static boolean wanted(android.content.Context c) { return false; }
  public static void setWanted(android.content.Context c, boolean on) { }
  public android.os.IBinder onBind(android.content.Intent i) { return null; }
}
""",
}


def test_direct_push_java_compiles_against_android_sdk():
    if ac.android_jar() is None:
        pytest.skip("no Android SDK installed")
    with tempfile.TemporaryDirectory() as out:
        result = ac.compile_sources(SOURCES, out, shims=SHIMS)
    assert result.returncode == 0, (result.stdout + "\n" + result.stderr)[-8000:]
