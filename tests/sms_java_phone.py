"""THE PHONE'S HALF OF THE DESKTOP->PHONE TEXTS CHAINS, RUN ON A JVM -- NOT IMITATED.

tests/sms_java_event.py runs the one Java call that turns an incoming text into an event. This runs
the three the OTHER direction depends on (2026-10-22: every desktop->phone test fed each side a
hand-made copy of the other side's data, which is how the phone->desktop notification bug lived for
months):

* `perform` -- `SmsOutbox.perform`, what `SignerRelayService.smsOutbox` calls for every `pcai:smsout:`
  event on its relay sockets: decrypt the desktop's request, claim it, send, sign the `{done:true}`
  receipt. MMS requests go through the REAL `SyncStore` / `SyncNet` / `SyncCrypto` against a local
  HTTP server standing in for the instance's files index and Blossom (tests/client/test_sms_mms_both_ways_e2e.py).
* `sweep` -- `SmsSweep.run` over a provider of the test's rows, sealed exactly as `SmsArchive.sweep`
  seals them (`Crypt` NIP-44 to self, `SmsOutbox.signed`): the phone's archive of a picture message.
* `block` -- `SmsPlugin.setBlocked` -> `Blocklist.set`: the answer the WebView's applyBlocksHere reads.
* `archived` -- `SmsArchived.absorb` over the Texts events its relay sockets delivered (what
  SignerRelayService does with every `pcai:smsarc:` record), then `ArchivedThreads.hidden` -- the
  question the native thread list asks of each conversation.

What is NOT the shipped code, and why -- each is a platform service with no JVM implementation:

* the radio: `SmsSender` / `MmsSender` are replaced at RUNTIME ONLY (the compile binds to the real
  ones) by recorders that answer the way the real ones answer an accepted send;
* `Context` / `SharedPreferences` (a JSON file, so a second JVM run is the same phone later),
  `ContentResolver` / `ContentValues` / `BlockedNumberContract` (the blocked-numbers provider, a set);
* Capacitor's `Plugin` / `PluginCall` / `JSObject` (the bridge; the PluginCall's arguments are the
  ones sms.js passed, recorded by the node harness).
Plus the shims tests/sms_java_event.py already documents (SignerKey, org.json, Log).
"""
import json
import os
import shutil
import subprocess
import tempfile

import androidcompile as ac
import sms_java_event as base

SMS = base.SMS
PHONE = os.path.join(ac.JAVA, "place", "poster", "app", "phone")

RUNTIME = {
    "android/content/Context.java": r"""
package android.content;
/** RUNTIME ONLY (tests/sms_java_phone.py): the members the code under test calls. */
public abstract class Context {
  public static final int MODE_PRIVATE = 0;
  public abstract SharedPreferences getSharedPreferences(String name, int mode);
  public abstract Context getApplicationContext();
  public abstract ContentResolver getContentResolver();
}
""",
    "android/content/ContentResolver.java": r"""
package android.content;
public abstract class ContentResolver {
  public abstract android.net.Uri insert(android.net.Uri uri, ContentValues values);
}
""",
    "android/content/ContentValues.java": r"""
package android.content;
public final class ContentValues {
  public final java.util.LinkedHashMap<String, Object> m = new java.util.LinkedHashMap<>();
  public ContentValues() { }
  public void put(String k, String v) { m.put(k, v); }
}
""",
    "android/provider/BlockedNumberContract.java": r"""
package android.provider;
import android.content.Context;
/** RUNTIME ONLY: the platform's blocked-numbers provider, as the fake phone's set. */
public class BlockedNumberContract {
  public static boolean canCurrentUserBlockNumbers(Context ctx) { return place.poster.app.sms.FakePhone.of(ctx).blockAvailable; }
  public static boolean isBlocked(Context ctx, String n) { return place.poster.app.sms.FakePhone.of(ctx).blocked.contains(n); }
  public static int unblock(Context ctx, String n) { return place.poster.app.sms.FakePhone.of(ctx).blocked.remove(n) ? 1 : 0; }
  public static class BlockedNumbers {
    public static final String COLUMN_ORIGINAL_NUMBER = "original_number";
    public static final android.net.Uri CONTENT_URI = null;
  }
}
""",
    "com/getcapacitor/JSObject.java": r"""
package com.getcapacitor;
public class JSObject extends org.json.JSONObject {
  public JSObject() { }
  @Override public JSObject put(String k, Object v) { try { super.put(k, v); } catch (Exception e) { } return this; }
  public JSObject put(String k, String v) { return put(k, (Object) v); }
  @Override public JSObject put(String k, boolean v) { return put(k, (Object) v); }
  @Override public JSObject put(String k, int v) { return put(k, (Object) v); }
  @Override public JSObject put(String k, long v) { return put(k, (Object) v); }
}
""",
    "com/getcapacitor/PluginCall.java": r"""
package com.getcapacitor;
/** RUNTIME ONLY: the arguments the WebView passed, and what the method resolved with. */
public class PluginCall {
  public final org.json.JSONObject data; public JSObject resolved;
  public PluginCall(org.json.JSONObject data) { this.data = data; }
  public String getString(String k, String d) { Object v = data.opt(k); return v instanceof String ? (String) v : d; }
  public Boolean getBoolean(String k, Boolean d) { Object v = data.opt(k); return v instanceof Boolean ? (Boolean) v : d; }
  public void resolve(JSObject o) { resolved = o; }
  public void resolve() { resolved = new JSObject(); }
  public void reject(String m) { resolved = new JSObject().put("rejected", m); }
}
""",
    "com/getcapacitor/Plugin.java": r"""
package com.getcapacitor;
public class Plugin {
  public static android.content.Context CTX;
  public android.content.Context getContext() { return CTX; }
}
""",
    # THE RADIO. Same names and Result shape as the shipped classes; a send is RECORDED and answered
    # the way SmsSender/MmsSender answer one the platform accepted.
    "place/poster/app/sms/SmsSender.java": r"""
package place.poster.app.sms;
import android.content.Context;
public final class SmsSender {
  public static final class Result { public boolean ok; public String error = ""; public android.net.Uri row;
    public int parts; public long sentAt; public boolean stored; }
  public static Result send(Context ctx, String to, String body) {
    FakePhone.of(ctx).radio("sms", to, body, null, null, null);
    Result r = new Result(); r.ok = true; r.parts = 1; r.sentAt = System.currentTimeMillis(); return r;
  }
}
""",
    "place/poster/app/sms/MmsSender.java": r"""
package place.poster.app.sms;
import android.content.Context;
public final class MmsSender {
  public static SmsSender.Result send(Context ctx, String to, String body, byte[] raw, String mime, String name) {
    FakePhone.of(ctx).radio("mms", to, body, raw, mime, name);
    SmsSender.Result r = new SmsSender.Result(); r.ok = true; r.sentAt = System.currentTimeMillis(); return r;
  }
}
""",
    "place/poster/app/sms/FakePhone.java": r"""
package place.poster.app.sms;
import android.content.*;
import java.nio.file.*;
import java.util.*;
import org.json.*;

/** One phone: its SharedPreferences, blocked-numbers list and radio log, persisted to a JSON file so
 *  that a second JVM run is the SAME phone later (a claim made on one relay's copy must still hold
 *  when another relay replays the request). */
public final class FakePhone extends Context {
  final Path file; final JSONObject state;
  public final Set<String> blocked = new LinkedHashSet<>();
  public boolean blockAvailable = true;
  public final JSONArray radio = new JSONArray();
  FakePhone(Path file) throws Exception {
    this.file = file;
    state = Files.exists(file) ? new JSONObject(new String(Files.readAllBytes(file), "UTF-8")) : new JSONObject();
    if (state.optJSONObject("prefs") == null) state.put("prefs", new JSONObject());
    JSONArray b = state.optJSONArray("blocked");
    if (b != null) for (int i = 0; i < b.length(); i++) blocked.add(b.optString(i));
    blockAvailable = state.optBoolean("blockAvailable", true);
  }
  public static FakePhone of(Context c) { return (FakePhone) c; }
  void save() throws Exception {
    JSONArray b = new JSONArray(); for (String n : blocked) b.put(n);
    state.put("blocked", b);
    Files.write(file, state.toString().getBytes("UTF-8"));
  }
  void radio(String kind, String to, String body, byte[] raw, String mime, String name) {
    try {
      JSONObject o = new JSONObject().put("kind", kind).put("to", to).put("body", body);
      if (raw != null) o.put("b64", Base64.getEncoder().encodeToString(raw)).put("mime", mime).put("name", name);
      radio.put(o);
    } catch (Exception e) { throw new RuntimeException(e); }
  }
  @Override public Context getApplicationContext() { return this; }
  @Override public ContentResolver getContentResolver() {
    return new ContentResolver() {
      @Override public android.net.Uri insert(android.net.Uri uri, ContentValues v) {
        Object n = v.m.get("original_number"); if (blockAvailable && n != null) blocked.add(String.valueOf(n)); return null; }
    };
  }
  @Override public SharedPreferences getSharedPreferences(String name, int mode) {
    try {
      JSONObject all = state.optJSONObject("prefs");
      if (all.optJSONObject(name) == null) all.put(name, new JSONObject());
      return new Prefs(all.optJSONObject(name));
    } catch (Exception e) { throw new RuntimeException(e); }
  }
  final class Prefs implements SharedPreferences {
    final JSONObject m; Prefs(JSONObject m) { this.m = m; }
    public Map<String, ?> getAll() { Map<String, Object> o = new LinkedHashMap<>(); for (Iterator<String> i = m.keys(); i.hasNext();) { String k = i.next(); o.put(k, m.opt(k)); } return o; }
    public String getString(String k, String d) { Object v = m.opt(k); return v instanceof String ? (String) v : d; }
    public Set<String> getStringSet(String k, Set<String> d) { return d; }
    public int getInt(String k, int d) { Object v = m.opt(k); return v instanceof Number ? ((Number) v).intValue() : d; }
    public long getLong(String k, long d) { Object v = m.opt(k); return v instanceof Number ? ((Number) v).longValue() : d; }
    public float getFloat(String k, float d) { Object v = m.opt(k); return v instanceof Number ? ((Number) v).floatValue() : d; }
    public boolean getBoolean(String k, boolean d) { Object v = m.opt(k); return v instanceof Boolean ? (Boolean) v : d; }
    public boolean contains(String k) { return m.has(k); }
    public Editor edit() { return new Ed(); }
    public void registerOnSharedPreferenceChangeListener(OnSharedPreferenceChangeListener l) { }
    public void unregisterOnSharedPreferenceChangeListener(OnSharedPreferenceChangeListener l) { }
    final class Ed implements Editor {
      final Map<String, Object> put = new LinkedHashMap<>(); final Set<String> rm = new HashSet<>(); boolean clear;
      public Editor putString(String k, String v) { put.put(k, v); return this; }
      public Editor putStringSet(String k, Set<String> v) { return this; }
      public Editor putInt(String k, int v) { put.put(k, (long) v); return this; }
      public Editor putLong(String k, long v) { put.put(k, v); return this; }
      public Editor putFloat(String k, float v) { put.put(k, (double) v); return this; }
      public Editor putBoolean(String k, boolean v) { put.put(k, v); return this; }
      public Editor remove(String k) { rm.add(k); return this; }
      public Editor clear() { clear = true; return this; }
      public boolean commit() {
        try {
          if (clear) { List<String> ks = new ArrayList<>(); for (Iterator<String> i = m.keys(); i.hasNext();) ks.add(i.next()); for (String k : ks) m.put(k, null); }
          for (String k : rm) m.put(k, null);
          for (Map.Entry<String, Object> e : put.entrySet()) m.put(e.getKey(), e.getValue());
          save(); return true;
        } catch (Exception e) { throw new RuntimeException(e); }
      }
      public void apply() { commit(); }
    }
  }
}
""",
}

DRIVER = r"""
package place.poster.app.sms;
import java.nio.file.*;
import java.util.*;
import org.json.*;

public class PhoneDrv {
  static String stdin() throws Exception { return new String(System.in.readAllBytes(), "UTF-8"); }

  public static void main(String[] a) throws Exception {
    place.poster.app.signer.SignerKey.SEC = place.poster.app.signer.Nostr.unhex(a[1]);
    FakePhone ctx = new FakePhone(Paths.get(a[2]));
    JSONObject out = new JSONObject();
    if (a[0].equals("configure")) {
      // What SyncPlugin.configure stores when the WebView signs in: where the instance and its
      // Blossom are, and the account's wrapped drive key. A background MMS needs all three.
      JSONObject c = new JSONObject(stdin());
      new place.poster.app.sync.SyncStore(ctx).configure(true, c.optString("api"), c.optString("media"),
          c.optString("mk"), "test phone", null);
    } else if (a[0].equals("perform")) {
      // EVERY event the relay sockets delivered, in order, each handed to SmsOutbox.perform the way
      // SignerRelayService.smsOutbox does; the non-null answers are what it publishes.
      JSONArray evs = new JSONArray(stdin());
      JSONArray published = new JSONArray(), asked = new JSONArray();
      for (int i = 0; i < evs.length(); i++) {
        JSONObject ev = evs.optJSONObject(i);
        asked.put(SmsOutbox.isRequest(ev));
        if (!SmsOutbox.isRequest(ev)) continue;
        JSONObject done = SmsOutbox.perform(ctx, ev);
        if (done != null) published.put(done);
      }
      out.put("isRequest", asked).put("published", published);
    } else if (a[0].equals("sweep")) {
      out = Sweep.run(ctx, new JSONObject(stdin()));
    } else if (a[0].equals("archived")) {
      // Every event the sockets delivered, in order, each offered to SmsArchived.absorb as
      // SignerRelayService does; then the native list's question, per (address, newest date).
      JSONObject in = new JSONObject(stdin());
      JSONArray evs = in.optJSONArray("events"), ask = in.optJSONArray("ask"), hidden = new JSONArray();
      int took = 0;
      for (int i = 0; i < evs.length(); i++) if (SmsArchived.absorb(ctx, evs.optJSONObject(i))) took++;
      ArchivedThreads t = SmsArchived.load(ctx);
      for (int i = 0; i < ask.length(); i++) {
        JSONArray q = ask.optJSONArray(i);
        hidden.put(t.hidden(q.optString(0), ((Number) q.opt(1)).longValue()));
      }
      out.put("absorbed", took).put("hidden", hidden);
    } else if (a[0].equals("block")) {
      ctx.blockAvailable = !"false".equals(a.length > 3 ? a[3] : "true");
      com.getcapacitor.Plugin.CTX = ctx;
      com.getcapacitor.PluginCall call = new com.getcapacitor.PluginCall(new JSONObject(stdin()));
      new SmsPlugin().setBlocked(call);
      JSONArray b = new JSONArray(); for (String n : ctx.blocked) b.put(n);
      out.put("resolved", call.resolved).put("list", b);
    }
    out.put("radio", ctx.radio);
    ctx.save();
    System.out.println(out.toString());
  }
}
"""

SWEEP = r"""
package place.poster.app.sms;
import java.util.*;
import org.json.*;
import place.poster.app.signer.*;
import place.poster.app.sync.SyncCrypto;

/** SmsSweep.run with SmsArchive's sealing (Crypt NIP-44 to self + SmsOutbox.signed) and a provider
 *  made of the test's rows. Blobs are sealed with SyncCrypto under the given drive key, so the hash
 *  the document names is the hash of real drive ciphertext. */
final class Sweep {
  static JSONObject run(android.content.Context ctx, JSONObject in) throws Exception {
    final byte[] sec = SignerKey.load(ctx), me = Nostr.pubkey(sec);
    final String pubHex = SmsOutbox.hex(me);
    final byte[] mk = Base64.getDecoder().decode(in.optString("mk"));
    final JSONArray rowsIn = in.optJSONArray("rows");
    final JSONObject names = in.optJSONObject("names") == null ? new JSONObject() : in.optJSONObject("names");
    final List<SmsMsg> rows = new ArrayList<>();
    final Map<Long, byte[]> bytes = new HashMap<>();
    for (int i = 0; i < rowsIn.length(); i++) {
      JSONObject r = rowsIn.optJSONObject(i);
      SmsMsg m = new SmsMsg();
      m.id = i + 1; m.address = r.optString("address"); m.body = r.optString("body", "");
      m.date = r.optLong("date", 0L); m.type = (int) r.optLong("type", 1L); m.mms = r.optBoolean("mms", false);
      JSONArray ps = r.optJSONArray("parts");
      if (ps != null) for (int k = 0; k < ps.length(); k++) {
        JSONObject p = ps.optJSONObject(k);
        SmsPart part = new SmsPart(); part.id = 100 * (i + 1) + k; part.ct = p.optString("ct");
        part.name = p.optString("name"); byte[] b = Base64.getDecoder().decode(p.optString("b64"));
        part.bytes = b.length; bytes.put(part.id, b); m.parts.add(part);
      }
      rows.add(m);
    }
    final JSONObject blobs = new JSONObject();
    final JSONArray events = new JSONArray();
    SmsSweep.Io io = new SmsSweep.Io() {
      public List<SmsMsg> since(long dateMs, int limit) {
        List<SmsMsg> o = new ArrayList<>(); for (SmsMsg m : rows) if (m.date > dateMs) o.add(m); return o; }
      public byte[] partBytes(SmsPart p) { return bytes.get(p.id); }
      public String putBlob(byte[] plain, String mime, String name) throws Exception {
        byte[] blob = SyncCrypto.encrypt(mk, plain);
        String sha = SyncCrypto.sha256hex(blob);
        blobs.put(sha, Base64.getEncoder().encodeToString(blob));
        return sha;
      }
      public JSONObject seal(String doc, String bodyJson) throws Exception {
        String ct = Crypt.nip44Encrypt(Crypt.conversationKey(sec, me), bodyJson, null);
        List<List<String>> tags = new ArrayList<>();
        tags.add(new ArrayList<>(Arrays.asList("d", doc))); tags.add(new ArrayList<>(Arrays.asList("l", "pcai-sms")));
        return SmsOutbox.signed(sec, pubHex, System.currentTimeMillis() / 1000L, 30078, tags, ct);
      }
      public long now() { return Long.MAX_VALUE / 4; }   // past every settle window
      // The phone's address book (PhoneBook.nameOf over the Contacts provider), as the test's map.
      public String contactName(String address) { return names.optString(address, ""); }
      // The sweep's high-water mark, kept in the phone's preferences as SmsArchive keeps it: a second
      // run is the same phone later, and re-reads only what is newer.
      public long mark() { return ctx.getSharedPreferences("pc_sms_sweep", 0).getLong("mark", 0L); }
      public void mark(long ms) { ctx.getSharedPreferences("pc_sms_sweep", 0).edit().putLong("mark", ms).commit(); }
    };
    SmsSweep.Report rep = SmsSweep.run(io, 100);
    SmsSweep.commit(io, rep);          // the relay accepted the batch (the test publishes every event)
    for (JSONObject ev : rep.events) events.put(ev);
    return new JSONObject().put("events", events).put("blobs", blobs).put("error", rep.error)
        .put("attachments", (long) rep.attachments).put("refused", (long) rep.refused);
  }
}
"""


def available():
    return base.available()


class Phone:
    """Compile once. `run(mode, sec_hex, state_path, stdin, *extra)` -> the driver's JSON answer."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="pc-sms-java-phone-")
        import test_android_signer_service_compiles as svc
        shims = dict(svc.SHIMS)
        shims["place/poster/app/signer/SignerKey.java"] = base.SIGNER_KEY_SHIM
        real = [os.path.join(SMS, f + ".java") for f in ("SmsOutbox", "SmsSweep", "SmsPlugin", "SmsArchived")]
        real.append(os.path.join(PHONE, "Blocklist.java"))
        r = ac.compile_sources(real, self.tmp, shims=shims)
        assert r.returncode == 0, "the phone half does not compile against android.jar:\n" \
            + (r.stdout[-2000:] + r.stderr[-6000:])
        rt_src = os.path.join(self.tmp, "rt-src")
        runtime = dict(base.JSON_RUNTIME)
        runtime.update(RUNTIME)
        # The driver is compiled HERE, against the runtime Context it instantiates (android.jar's
        # is abstract over a hundred members), never against the platform.
        runtime["place/poster/app/sms/PhoneDrv.java"] = DRIVER
        runtime["place/poster/app/sms/Sweep.java"] = SWEEP
        for rel, body in runtime.items():
            p = os.path.join(rt_src, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
        self.rt = os.path.join(self.tmp, "rt")
        classes = os.path.join(self.tmp, "classes")
        # The runtime set is compiled against the app's classes + android.jar, its own sources
        # winning (a source named on the command line beats the same class on the classpath).
        r = subprocess.run(["javac", "-nowarn", "-d", self.rt, "-cp",
                            os.pathsep.join([classes, ac.android_jar()]),
                            os.path.join(base.STUBS, "android", "util", "Log.java")]
                           + [os.path.join(rt_src, rel) for rel in runtime],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-4000:]
        self.cp = os.pathsep.join([self.rt, classes, ac.android_jar()])

    def run(self, mode, sec_hex, state, stdin, *extra):
        r = subprocess.run(["java", "-cp", self.cp, "place.poster.app.sms.PhoneDrv", mode, sec_hex,
                            state] + list(extra), input=json.dumps(stdin),
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-4000:]
        return json.loads(r.stdout.strip().splitlines()[-1])

    def argv(self, mode, sec_hex, state, *extra):
        """The command line, for the node harness to call mid-run (sms_sim.js phoneArgv/sweepArgv/blockArgv)."""
        return ["java", "-cp", self.cp, "place.poster.app.sms.PhoneDrv", mode, sec_hex, state] + list(extra)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
