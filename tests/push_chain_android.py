"""THE PHONE HALF OF THE PUSH CHAIN: the SHIPPED Java, run on a plain JVM, fed the server's real bytes.

What runs, unmodified, from mobile/android/.../app/:
  * `push/PushEventService.java` — the one renderer (`deliver` → `show` → `openIntent`);
  * `push/DirectPushStore.java`  — the phone-local type filter (`allowsType`) and the delivery receipts;
  * `push/ClientNotified.java`   — the client-already-said-it window and the own-sent wrap ids;
  * `ringtone/RingtoneRules.java`, `home/LaunchView.java` — the channel choice and the tap's parking;
  * `DirectPushService`'s socket `onMessage` and `sendAck` — SLICED out of the real file (the rest of
    that class is OkHttp/Service plumbing) and wrapped in a class carrying the fields they read.

What is a stand-in: the Android platform classes. They are written here to RECORD, not to type-check
(compile coverage against the real android.jar is tests/test_android_direct_push_compiles.py): every
`NotificationManager.notify(tag, id, n)` is captured with the title, text, channel, category,
full-screen flag and the content intent's extras — i.e. exactly what Android would put in the shade
and what a tap would hand `MainActivity`. org.json is the working copy from tests/sms_java_event.py,
extended with the three readers this path uses (Android's own jar only has throwing stubs).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "mobile/android/app/src/main/java/place/poster/app"


def available() -> bool:
    return bool(shutil.which("javac") and shutil.which("java"))


def _json_runtime() -> dict:
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    from sms_java_event import JSON_RUNTIME
    rt = dict(JSON_RUNTIME)
    # Only what the shared stand-in lacks: sms_java_event grows too (2026-10-22 it gained isNull/optJSONObject/
    # optBoolean and a JSONObject.NULL with its own toString), and a duplicate method is a javac error in BOTH harnesses.
    wanted = {
        "public JSONObject optJSONObject(String k)":
            "  public JSONObject optJSONObject(String k) { Object v = m.get(k); return v instanceof JSONObject ? (JSONObject) v : null; }\n",
        "public boolean isNull(String k)": "  public boolean isNull(String k) { return m.get(k) == null; }\n",
        "public boolean optBoolean(String k, boolean f)":
            "  public boolean optBoolean(String k, boolean f) { Object v = m.get(k); if (v instanceof Boolean) return (Boolean) v;\n"
            "    if (v instanceof String) { if (\"true\".equalsIgnoreCase((String) v)) return true; if (\"false\".equalsIgnoreCase((String) v)) return false; } return f; }\n",
    }
    src = rt["org/json/JSONObject.java"]
    extra = "".join(body for sig, body in wanted.items() if sig not in src)
    anchor = src.rfind("  @Override public String toString() {")      # the class's own, after NULL's
    assert anchor > 0
    rt["org/json/JSONObject.java"] = src[:anchor] + extra + src[anchor:]
    return rt


def _home_constants() -> str:
    src = (APP / "home/HomeActivity.java").read_text()
    lines = [m.group(0) for m in re.finditer(r'public static final String EXTRA_VIEW(?:_AT)? = "[^"]+";', src)]
    assert len(lines) == 2, "HomeActivity's EXTRA_VIEW constants moved — re-read this harness"
    return "\n  ".join(lines)


STUBS = {
    "android/content/Context.java": r"""
package android.content;
public class Context {
  public static final int MODE_PRIVATE = 0;
  public static final String NOTIFICATION_SERVICE = "notification";
  public final java.util.Map<String, SharedPreferences> prefs = new java.util.HashMap<>();
  public android.app.NotificationManager nm = new android.app.NotificationManager();
  public SharedPreferences getSharedPreferences(String name, int mode) {
    return prefs.computeIfAbsent(name, k -> new SharedPreferences.Mem()); }
  public Object getSystemService(String name) { return NOTIFICATION_SERVICE.equals(name) ? nm : null; }
  public int checkSelfPermission(String p) { return android.content.pm.PackageManager.PERMISSION_GRANTED; }
  public String getPackageName() { return "place.poster.app"; }
}
""",
    "android/content/SharedPreferences.java": r"""
package android.content;
public interface SharedPreferences {
  String getString(String k, String d); boolean getBoolean(String k, boolean d); Editor edit();
  interface Editor { Editor putString(String k, String v); Editor putBoolean(String k, boolean v);
    Editor remove(String k); void apply(); boolean commit(); }
  final class Mem implements SharedPreferences {
    final java.util.Map<String, Object> m = new java.util.HashMap<>();
    public String getString(String k, String d) { Object v = m.get(k); return v == null ? d : (String) v; }
    public boolean getBoolean(String k, boolean d) { Object v = m.get(k); return v == null ? d : (Boolean) v; }
    public Editor edit() { final Mem self = this; return new Editor() {
      public Editor putString(String k, String v) { self.m.put(k, v); return this; }
      public Editor putBoolean(String k, boolean v) { self.m.put(k, v); return this; }
      public Editor remove(String k) { self.m.remove(k); return this; }
      public void apply() { } public boolean commit() { return true; } }; }
  }
}
""",
    "android/content/Intent.java": r"""
package android.content;
public class Intent {
  public static final int FLAG_ACTIVITY_NEW_TASK = 0x10000000;
  public static final int FLAG_ACTIVITY_CLEAR_TOP = 0x04000000;
  public final java.util.Map<String, Object> extras = new java.util.LinkedHashMap<>();
  public Class<?> target; public int flags;
  public Intent(Context ctx, Class<?> cls) { target = cls; }
  public Intent setFlags(int f) { flags = f; return this; }
  public Intent putExtra(String k, String v) { extras.put(k, v); return this; }
  public Intent putExtra(String k, long v) { extras.put(k, v); return this; }
  public String getStringExtra(String k) { Object v = extras.get(k); return v instanceof String ? (String) v : null; }
  public long getLongExtra(String k, long d) { Object v = extras.get(k); return v instanceof Long ? (Long) v : d; }
}
""",
    "android/content/pm/PackageManager.java": r"""
package android.content.pm;
public class PackageManager { public static final int PERMISSION_GRANTED = 0; }
""",
    "android/app/Service.java": "package android.app;\npublic class Service extends android.content.Context { }\n",
    "android/app/PendingIntent.java": r"""
package android.app;
public class PendingIntent {
  public static final int FLAG_UPDATE_CURRENT = 0x08000000, FLAG_IMMUTABLE = 0x04000000;
  public final android.content.Intent intent; public final int requestCode;
  PendingIntent(int rc, android.content.Intent i) { requestCode = rc; intent = i; }
  public static PendingIntent getActivity(android.content.Context c, int rc, android.content.Intent i, int f) { return new PendingIntent(rc, i); }
}
""",
    "android/app/Notification.java": r"""
package android.app;
public class Notification {
  public static final String CATEGORY_CALL = "call";
  public static final int PRIORITY_HIGH = 1;
  public String channel, title, text, category; public PendingIntent content, fullScreen; public boolean autoCancel;
  public static class Builder {
    final Notification n = new Notification();
    public Builder(android.content.Context c, String channel) { n.channel = channel; }
    public Builder(android.content.Context c) { }
    public Builder setContentTitle(CharSequence s) { n.title = String.valueOf(s); return this; }
    public Builder setContentText(CharSequence s) { n.text = String.valueOf(s); return this; }
    public Builder setSmallIcon(int i) { return this; }
    public Builder setAutoCancel(boolean b) { n.autoCancel = b; return this; }
    public Builder setContentIntent(PendingIntent p) { n.content = p; return this; }
    public Builder setOngoing(boolean b) { return this; }
    public Builder setCategory(String c) { n.category = c; return this; }
    public Builder setFullScreenIntent(PendingIntent p, boolean hi) { n.fullScreen = p; return this; }
    public Builder setPriority(int p) { return this; }
    public Notification build() { return n; }
  }
}
""",
    "android/app/NotificationChannel.java": r"""
package android.app;
public class NotificationChannel {
  final String id; final int importance; android.net.Uri sound = android.net.Uri.parse("default");
  public NotificationChannel(String id, CharSequence name, int importance) { this.id = id; this.importance = importance; }
  public void setDescription(String d) { }
  public void setSound(android.net.Uri u, android.media.AudioAttributes a) { sound = u; }
  public android.net.Uri getSound() { return sound; }
  public int getImportance() { return importance; }
  public String getId() { return id; }
}
""",
    "android/app/NotificationManager.java": r"""
package android.app;
public class NotificationManager {
  public static final int IMPORTANCE_UNSPECIFIED = -1000, IMPORTANCE_NONE = 0, IMPORTANCE_LOW = 2,
      IMPORTANCE_DEFAULT = 3, IMPORTANCE_HIGH = 4;
  /** The shade: keyed on (tag, id), exactly as Android keys it — a repeat REPLACES. */
  public final java.util.LinkedHashMap<String, Notification> shade = new java.util.LinkedHashMap<>();
  public final java.util.List<String> posted = new java.util.ArrayList<>();
  final java.util.Map<String, NotificationChannel> channels = new java.util.HashMap<>();
  public void notify(String tag, int id, Notification n) { String k = tag + "|" + id; shade.remove(k); shade.put(k, n); posted.add(k); }
  public boolean areNotificationsEnabled() { return true; }
  public NotificationChannel getNotificationChannel(String id) { return channels.get(id); }
  public void createNotificationChannel(NotificationChannel c) { channels.putIfAbsent(c.getId(), c); }
}
""",
    "android/os/Build.java": r"""
package android.os;
public class Build {
  public static class VERSION { public static int SDK_INT = 34; }
  public static class VERSION_CODES { public static final int LOLLIPOP = 21, M = 23, O = 26; }
}
""",
    "android/net/Uri.java": r"""
package android.net;
public class Uri { final String s; Uri(String s) { this.s = s; } public static Uri parse(String s) { return new Uri(s); } public String toString() { return s; } }
""",
    "android/media/AudioAttributes.java": r"""
package android.media;
public class AudioAttributes {
  public static final int USAGE_NOTIFICATION = 5, CONTENT_TYPE_SONIFICATION = 4;
  public static class Builder { public Builder setUsage(int u) { return this; } public Builder setContentType(int c) { return this; }
    public AudioAttributes build() { return new AudioAttributes(); } }
}
""",
    "android/security/keystore/KeyProperties.java": r"""
package android.security.keystore;
public class KeyProperties { public static final String KEY_ALGORITHM_AES = "AES", BLOCK_MODE_GCM = "GCM", ENCRYPTION_PADDING_NONE = "NoPadding";
  public static final int PURPOSE_ENCRYPT = 1, PURPOSE_DECRYPT = 2; }
""",
    "android/security/keystore/KeyGenParameterSpec.java": r"""
package android.security.keystore;
public class KeyGenParameterSpec implements java.security.spec.AlgorithmParameterSpec {
  public static class Builder { public Builder(String a, int p) { } public Builder setBlockModes(String... m) { return this; }
    public Builder setEncryptionPaddings(String... p) { return this; } public Builder setKeySize(int s) { return this; }
    public Builder setUserAuthenticationRequired(boolean b) { return this; } public KeyGenParameterSpec build() { return new KeyGenParameterSpec(); } }
}
""",
    "android/util/Base64.java": r"""
package android.util;
public class Base64 { public static final int NO_WRAP = 2;
  public static String encodeToString(byte[] b, int f) { return java.util.Base64.getEncoder().encodeToString(b); }
  public static byte[] decode(String s, int f) { return java.util.Base64.getDecoder().decode(s); } }
""",
    "android/R.java": r"""
package android;
public final class R { public static final class drawable { public static final int sym_action_chat = 1; } }
""",
    "okhttp3/WebSocket.java": "package okhttp3;\npublic interface WebSocket { boolean send(String text); }\n",
    "place/poster/app/R.java": r"""
package place.poster.app;
public final class R { public static final class raw { public static final int posterchan_alert = 1, posterchan_chime = 2; } }
""",
    "place/poster/app/MainActivity.java": "package place.poster.app;\npublic class MainActivity { }\n",
    "place/poster/app/RunningNote.java": r"""
package place.poster.app;
public final class RunningNote { public static void refresh(android.content.Context c) { } }
""",
}


def _socket_class() -> str:
    """`DirectPushService`'s real onMessage + sendAck, wrapped with the fields they read."""
    src = (APP / "push/DirectPushService.java").read_text()
    a = src.index("            @Override public void onMessage(WebSocket webSocket, String text) {")
    b = src.index("            @Override public void onClosing(")
    on_message = src[a:b].replace("@Override ", "")
    c = src.index("    private static void sendAck(")
    d = src.index("    private void failed(")
    send_ack = src[c:d]
    const = next(l for l in src.splitlines() if "MAX_MESSAGE_BYTES =" in l)
    return """
package place.poster.app.push;
import okhttp3.WebSocket;
import org.json.JSONObject;
import place.poster.app.RunningNote;
/** Harness wrapper (tests/push_chain_android.py): the SHIPPED onMessage/sendAck, sliced verbatim. */
public class DirectPushService extends android.app.Service {
  int generation = 1, failures = 3; boolean running = true, connected = true; String lastError = "";
  %s
  public class Listener { final int mine = 1;
%s
  }
%s
}
""" % (const.strip(), on_message, send_ack)


DRIVER = r"""
package place.poster.app.push;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import org.json.JSONArray;
import org.json.JSONObject;
import place.poster.app.home.HomeActivity;
import place.poster.app.home.LaunchView;

/** Feeds each captured socket frame to the shipped onMessage on ONE phone and reports the shade. */
public class ChainDriver {
  public static void main(String[] a) throws Exception {
    DirectPushService phone = new DirectPushService();
    DirectPushService.Listener socket = phone.new Listener();
    final List<String> acks = new ArrayList<>();
    okhttp3.WebSocket ws = text -> { acks.add(text); return true; };
    StringBuilder out = new StringBuilder();
    for (String line : Files.readAllLines(Paths.get(a[0]), StandardCharsets.UTF_8)) {
      if (line.isEmpty()) continue;
      String[] f = line.split("\t", 5);
      String label = f[0], prefs = f[1], clientDm = f[2], sentWid = f[3], frame = f[4];
      long now = System.currentTimeMillis();
      if (!f[1].equals("=")) {               // "=": keep this phone's state from the previous line
        ClientNotified.clear();
        phone.nm.shade.clear();
        DirectPushStore.setTypePrefs(phone, prefs.equals("-") ? "" : prefs);
        if (!clientDm.equals("-")) ClientNotified.dm(now - Long.parseLong(clientDm));
        if (!sentWid.equals("-")) ClientNotified.sent(sentWid, now - 1000);
      }
      phone.nm.posted.clear(); acks.clear();
      socket.onMessage(ws, frame);
      JSONArray cards = new JSONArray();
      for (Map.Entry<String, android.app.Notification> e : phone.nm.shade.entrySet()) {
        android.app.Notification n = e.getValue();
        android.content.Intent tap = n.content == null ? null : n.content.intent;
        JSONObject c = new JSONObject();
        String[] key = e.getKey().split("\\|", 2);
        c.put("tag", key[0]); c.put("id", Long.parseLong(key[1]));
        c.put("title", n.title); c.put("text", n.text); c.put("channel", String.valueOf(n.channel));
        c.put("category", String.valueOf(n.category)); c.put("fullScreen", n.fullScreen != null);
        c.put("posted_now", phone.nm.posted.contains(e.getKey()));
        if (tap != null) {
          String view = tap.getStringExtra(HomeActivity.EXTRA_VIEW);
          c.put("view", view);
          c.put("activity", tap.target.getName());
          // The TAP, five minutes later (a notification sits in the shade): MainActivity hands the
          // extra to LaunchView.deliver and HomePlugin.consumeLaunchView takes it.
          long tapAt = now + 5 * 60_000L;
          LaunchView.clear();
          boolean parked = LaunchView.deliver(view, 0, false, tapAt);
          c.put("landed", parked ? LaunchView.take(tapAt + 50) : "");
        }
        cards.put(c);
      }
      JSONArray ackIds = new JSONArray();
      for (String s : acks) ackIds.put(new JSONObject(s).optString("id", ""));
      JSONObject r = new JSONObject();
      r.put("label", label); r.put("cards", cards); r.put("acks", ackIds);
      out.append(r.toString()).append('\n');
    }
    System.out.print(out);
  }
}
"""


class Phone:
    """Compile once (the shipped sources + the recording platform), then `run(lines)`."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="pc-push-chain-")
        src = Path(self.tmp) / "src"
        files = dict(STUBS)
        files.update(_json_runtime())
        files["place/poster/app/home/HomeActivity.java"] = (
            "package place.poster.app.home;\npublic class HomeActivity {\n  %s\n}\n" % _home_constants())
        files["place/poster/app/push/DirectPushService.java"] = _socket_class()
        files["place/poster/app/push/ChainDriver.java"] = DRIVER
        paths = []
        for rel, body in files.items():
            p = src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            paths.append(str(p))
        for rel in ("push/PushEventService.java", "push/DirectPushStore.java", "push/ClientNotified.java",
                    "ringtone/RingtoneRules.java", "home/LaunchView.java"):
            p = src / "place/poster/app" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(APP / rel, p)
            paths.append(str(p))
        self.classes = os.path.join(self.tmp, "classes")
        r = subprocess.run(["javac", "-nowarn", "-encoding", "UTF-8", "-d", self.classes] + paths,
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]

    def run(self, lines):
        """`lines`: (label, local_prefs|None, client_dm_ms_ago|None, sent_wid|None, frame_text).

        local_prefs "=" keeps the phone exactly as the previous line left it (a replayed frame)."""
        inp = Path(self.tmp) / "frames.tsv"
        with open(inp, "w", encoding="utf-8") as f:
            for label, prefs, dm_ago, wid, frame in lines:
                assert "\t" not in frame and "\n" not in frame
                f.write("\t".join([label, "-" if prefs is None else prefs,
                                   "-" if dm_ago is None else str(dm_ago),
                                   "-" if wid is None else wid, frame]) + "\n")
        r = subprocess.run(["java", "-Dfile.encoding=UTF-8", "-cp", self.classes,
                            "place.poster.app.push.ChainDriver", str(inp)],
                           capture_output=True, timeout=120)
        assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-4000:]
        return {d["label"]: d for d in (json.loads(x) for x in r.stdout.decode("utf-8").splitlines() if x)}

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
