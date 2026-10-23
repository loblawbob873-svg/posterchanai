"""THE PHONE'S OWN ARCHIVE EVENT FOR AN INCOMING TEXT, BUILT BY THE SHIPPED JAVA.

`SmsDeliverReceiver` hands every incoming text to `SignerRelayService.archiveIncoming`, which seals it
with `SmsOutbox.archiveIncoming` and sends it on the signer's relay sockets. That event is the ONLY
thing a desktop ever learns about a text that arrived while PosterChan was not open on the phone, and
until 2026-10-22 no test had ever produced one: the desktop notification suites fed the client a
plaintext, JS-shaped document that the phone never sends. Reported as "I am not seeing any SMS
notifications on OS and windows app ... that should have had a regression test a long time ago".

This compiles `SmsOutbox` (and everything it imports) against the REAL android.jar via
tests/androidcompile.py, then RUNS `archiveIncoming` on a plain JVM. Running needs three things the
android.jar on this box only has as throwing stubs, supplied on the runtime classpath AHEAD of it:

* `SignerKey` -- the Keystore-sealed key. Shimmed to return a fixture secret (compile AND run); the
  real class is pure Keystore plumbing and is not what is under test.
* `org.json.JSONObject/JSONArray` -- android.jar's copies throw "Stub!". Replaced at RUNTIME ONLY
  (the compile still binds to Android's real signatures) by a small working copy whose string quoting
  follows Android's JSONStringer (`/` escaped, control characters as \\u00XX).
* `android.util.Log` -- the signature-only stub from tests/androidstubs.

Everything else -- Nostr (BIP-340 signing, event id), Crypt (NIP-44 v2), SmsKeys.docId -- is the
shipped code, executed.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import androidcompile as ac  # noqa: E402

SMS = os.path.join(ac.JAVA, "place", "poster", "app", "sms")
STUBS = os.path.join(ac.ROOT, "tests", "androidstubs")

SIGNER_KEY_SHIM = r"""
package place.poster.app.signer;
import android.content.Context;
/** Test shim (tests/sms_java_event.py): the account key the real class unseals from the Keystore. */
public final class SignerKey {
  private SignerKey() { }
  public static byte[] SEC = null;
  public static boolean have(Context ctx) { return SEC != null; }
  public static byte[] load(Context ctx) { return SEC == null ? null : SEC.clone(); }
  public static String pubkey(Context ctx) { return SEC == null ? null : Nostr.hex(Nostr.pubkey(SEC)); }
  public static boolean exposed(Context ctx) { return false; }
  public static void setExposed(Context ctx, boolean on) { }
  public static String store(Context ctx, byte[] sec) { SEC = sec.clone(); return pubkey(ctx); }
  public static void clear(Context ctx) { SEC = null; }
}
"""

JSON_RUNTIME = {
    "org/json/JSONException.java": r"""
package org.json;
public class JSONException extends Exception { public JSONException(String s) { super(s); } }
""",
    "org/json/JSONObject.java": r"""
package org.json;
import java.util.*;
public class JSONObject {
  final LinkedHashMap<String,Object> m = new LinkedHashMap<>();
  public JSONObject() { }
  public JSONObject(String s) throws JSONException { Object v = new Json(s).value(); if (!(v instanceof JSONObject)) throw new JSONException("not an object"); m.putAll(((JSONObject) v).m); }
  public JSONObject put(String k, Object v) throws JSONException { if (v == null) m.remove(k); else m.put(k, v); return this; }
  public JSONObject put(String k, long v) throws JSONException { m.put(k, v); return this; }
  public JSONObject put(String k, int v) throws JSONException { m.put(k, v); return this; }
  public JSONObject put(String k, double v) throws JSONException { m.put(k, v); return this; }
  public JSONObject put(String k, boolean v) throws JSONException { m.put(k, v); return this; }
  public Object opt(String k) { return m.get(k); }
  public boolean has(String k) { return m.containsKey(k); }
  public String optString(String k) { return optString(k, ""); }
  public String optString(String k, String f) { Object v = m.get(k); return v == null ? f : String.valueOf(v); }
  public long optLong(String k, long f) { Object v = m.get(k); return v instanceof Number ? ((Number) v).longValue() : f; }
  public int length() { return m.size(); }
  public Iterator<String> keys() { return m.keySet().iterator(); }
  @Override public String toString() {
    StringBuilder b = new StringBuilder("{"); boolean first = true;
    for (Map.Entry<String,Object> e : m.entrySet()) { if (!first) b.append(','); first = false;
      b.append(quote(e.getKey())).append(':').append(Json.write(e.getValue())); }
    return b.append('}').toString();
  }
  /** Android's JSONStringer.string(), character for character. */
  public static String quote(String s) {
    StringBuilder o = new StringBuilder("\"");
    for (int i = 0; i < s.length(); i++) { char c = s.charAt(i);
      switch (c) {
        case '"': case '\\': case '/': o.append('\\').append(c); break;
        case '\t': o.append("\\t"); break; case '\b': o.append("\\b"); break;
        case '\n': o.append("\\n"); break; case '\r': o.append("\\r"); break; case '\f': o.append("\\f"); break;
        default: if (c <= 0x1F) o.append(String.format("\\u%04x", (int) c)); else o.append(c);
      } }
    return o.append('"').toString();
  }
}
""",
    "org/json/JSONArray.java": r"""
package org.json;
import java.util.*;
public class JSONArray {
  final ArrayList<Object> a = new ArrayList<>();
  public JSONArray() { }
  public JSONArray(String s) throws JSONException { Object v = new Json(s).value(); if (!(v instanceof JSONArray)) throw new JSONException("not an array"); a.addAll(((JSONArray) v).a); }
  public JSONArray put(Object v) { a.add(v); return this; }
  public JSONArray put(long v) { a.add(v); return this; }
  public JSONArray put(int v) { a.add(v); return this; }
  public int length() { return a.size(); }
  public Object get(int i) { return a.get(i); }
  public String optString(int i) { Object v = i < a.size() ? a.get(i) : null; return v == null ? "" : String.valueOf(v); }
  public String optString(int i, String f) { Object v = i < a.size() ? a.get(i) : null; return v == null ? f : String.valueOf(v); }
  public JSONArray optJSONArray(int i) { Object v = i < a.size() ? a.get(i) : null; return v instanceof JSONArray ? (JSONArray) v : null; }
  public JSONObject optJSONObject(int i) { Object v = i < a.size() ? a.get(i) : null; return v instanceof JSONObject ? (JSONObject) v : null; }
  @Override public String toString() {
    StringBuilder b = new StringBuilder("[");
    for (int i = 0; i < a.size(); i++) { if (i > 0) b.append(','); b.append(Json.write(a.get(i))); }
    return b.append(']').toString();
  }
}
""",
    "org/json/Json.java": r"""
package org.json;
/** Minimal reader/writer behind the runtime org.json copy (tests/sms_java_event.py). */
final class Json {
  private final String s; private int i = 0;
  Json(String s) { this.s = s; }
  static String write(Object v) {
    if (v instanceof String) return JSONObject.quote((String) v);
    return String.valueOf(v);
  }
  private void ws() { while (i < s.length() && Character.isWhitespace(s.charAt(i))) i++; }
  Object value() throws JSONException {
    ws(); if (i >= s.length()) throw new JSONException("eof");
    char c = s.charAt(i);
    if (c == '{') { i++; JSONObject o = new JSONObject(); ws(); if (s.charAt(i) == '}') { i++; return o; }
      while (true) { ws(); String k = (String) value(); ws(); i++; o.put(k, value()); ws();
        char d = s.charAt(i++); if (d == '}') return o; } }
    if (c == '[') { i++; JSONArray a = new JSONArray(); ws(); if (s.charAt(i) == ']') { i++; return a; }
      while (true) { a.put(value()); ws(); char d = s.charAt(i++); if (d == ']') return a; } }
    if (c == '"') { i++; StringBuilder b = new StringBuilder();
      while (true) { char d = s.charAt(i++); if (d == '"') return b.toString();
        if (d == '\\') { char e = s.charAt(i++);
          switch (e) { case 'n': b.append('\n'); break; case 't': b.append('\t'); break; case 'r': b.append('\r'); break;
            case 'b': b.append('\b'); break; case 'f': b.append('\f'); break;
            case 'u': b.append((char) Integer.parseInt(s.substring(i, i + 4), 16)); i += 4; break;
            default: b.append(e); } }
        else b.append(d); } }
    int st = i; while (i < s.length() && "{}[],: \t\r\n".indexOf(s.charAt(i)) < 0) i++;
    String t = s.substring(st, i);
    if (t.equals("true")) return Boolean.TRUE; if (t.equals("false")) return Boolean.FALSE; if (t.equals("null")) return null;
    if (t.contains(".") || t.contains("e") || t.contains("E")) return Double.parseDouble(t);
    return Long.parseLong(t);
  }
}
""",
}

DRIVER = r"""
package place.poster.app.sms;
public class SmsEventDrv {
  public static void main(String[] a) throws Exception {
    place.poster.app.signer.SignerKey.SEC = place.poster.app.signer.Nostr.unhex(a[0]);
    org.json.JSONObject ev = SmsOutbox.archiveIncoming(null, a[1], a[2], Long.parseLong(a[3]),
                                                       Long.parseLong(a[4]));
    System.out.println(ev == null ? "null" : ev.toString());
  }
}
"""


def available():
    return bool(shutil.which("javac") and shutil.which("java") and ac.android_jar()
                and os.path.isdir(SMS))


class Builder:
    """Compile once; `event(sec_hex, from, body, when_ms)` runs the shipped archiveIncoming."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="pc-sms-java-event-")
        drv = os.path.join(self.tmp, "SmsEventDrv.java")
        with open(drv, "w", encoding="utf-8") as f:
            f.write(DRIVER)
        # The library shims (okhttp, the MMS transport, MainActivity, music) are the signer-service
        # compile's own set -- ONE copy; SmsOutbox reaches them transitively, never on this path.
        import test_android_signer_service_compiles as svc
        shims = dict(svc.SHIMS)
        shims["place/poster/app/signer/SignerKey.java"] = SIGNER_KEY_SHIM
        r = ac.compile_sources([os.path.join(SMS, "SmsOutbox.java"), drv], self.tmp, shims=shims)
        assert r.returncode == 0, "SmsOutbox does not compile against android.jar:\n" + r.stderr[-4000:]
        rt_src = os.path.join(self.tmp, "rt-src")
        for rel, body in JSON_RUNTIME.items():
            p = os.path.join(rt_src, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(body)
        self.rt = os.path.join(self.tmp, "rt")
        r = subprocess.run(["javac", "-nowarn", "-d", self.rt,
                            os.path.join(STUBS, "android", "util", "Log.java")]
                           + [os.path.join(rt_src, rel) for rel in JSON_RUNTIME],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]
        self.cp = os.pathsep.join([self.rt, os.path.join(self.tmp, "classes"), ac.android_jar()])

    def event(self, sec_hex, sender, body, when_ms, received_ms=0):
        """`when_ms` is the carrier's stamp (SmsMessage.getTimestampMillis); `received_ms` is when the
        phone got it (SignerRelayService.archiveIncoming records System.currentTimeMillis())."""
        r = subprocess.run(["java", "-cp", self.cp, "place.poster.app.sms.SmsEventDrv",
                            sec_hex, sender, body, str(int(when_ms)), str(int(received_ms))],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-4000:]
        out = r.stdout.strip().splitlines()[-1]
        assert out != "null", "SmsOutbox.archiveIncoming returned null (sealing failed)"
        return json.loads(out)

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
