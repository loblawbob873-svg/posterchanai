"""The Android sweep, RUN — not compiled and read.

This is the half of folder sync that works while the screen is off, and until now it had never been
executed anywhere except a phone. It could be compiled, and its reconciler could be held to the
JavaScript one decision for decision (tests/test_android_reconcile_parity.py), and that was all: the
sweep itself built its own SAF handle and its own HTTP client, so nothing could drive it.

Now it takes them (see SyncIo), and this drives a whole one: device A uploads a folder, publishes its
record, and device B — a different journal, an empty disk, its own chunk size — reads every device's
record, merges them, downloads the lot and verifies it. Real SyncCrypto (NIP-44 seals and AES-GCM
over a real master key), real SyncReconcile, real Journal, real chunking. The store is an in-memory
relay that keys documents the way the server does, `pcai:sync:<pair>:<device>`, so a sweep that
writes the wrong document fails here.

What it does NOT cover: SAF itself, the real HTTP client, and Keystore. Those are the three things
only a phone can answer, and they are named here so nobody mistakes this for having run on one.
"""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
JAVA = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app", "sync")
STUBS = os.path.join(ROOT, "tests", "androidstubs")

SRC = [os.path.join(JAVA, f + ".java") for f in
       ("SyncStore", "NativeSweep", "SyncDiff", "SyncReconcile", "Json", "Excludes", "SyncCrypto",
        "NativeRunner", "FolderSyncPlugin", "SyncClock", "SyncCheckWorker", "SafFs", "SyncNet",
        "SyncIo", "SyncWork", "SyncService", "SyncTickReceiver")]

# A world the sweep can be run in: a folder on a HashMap, and a relay that behaves like the endpoint.
WORLD = r"""
package place.poster.app.sync;

import java.util.*;

/** A folder in memory. Every rule the sweep depends on — part files, trash, hashes — behaves as the
 *  SAF adapter does, because those are what a resumed or interrupted transfer turns on. */
class FakeFs implements SyncIo.Files {
    final Map<String, byte[]> disk = new LinkedHashMap<String, byte[]>();
    final Map<String, byte[]> parts = new LinkedHashMap<String, byte[]>();
    final List<String> trashed = new ArrayList<String>();
    long mtime = 1000L;

    public SafFs.Scan scan(boolean hash, long maxBytes, List<String> excludes) {
        SafFs.Scan s = new SafFs.Scan();
        for (Map.Entry<String, byte[]> e : disk.entrySet()) {
            Map<String, Object> m = new LinkedHashMap<String, Object>();
            m.put("size", (long) e.getValue().length);
            m.put("mtime", mtime);
            if (hash) m.put("sha", SyncCrypto.sha256hex(e.getValue()));
            s.files.put(e.getKey(), m);
        }
        return s;
    }
    String shortRead = null;              // this path reads back fewer bytes than the scan saw
    public byte[] readAll(String rel) {
        byte[] b = disk.get(rel);
        if (rel.equals(shortRead) && b != null) return Arrays.copyOfRange(b, 0, Math.max(1, b.length - 20));
        return b;
    }
    public byte[] readRange(String rel, long off, int len) {
        byte[] b = disk.get(rel);
        int from = (int) off, to = Math.min(b.length, from + len);
        return Arrays.copyOfRange(b, from, to);
    }
    public void writePart(String rel, long off, byte[] bytes) {
        byte[] cur = parts.get(rel);
        int need = (int) off + bytes.length;
        byte[] next = new byte[Math.max(cur == null ? 0 : cur.length, need)];
        if (cur != null) System.arraycopy(cur, 0, next, 0, cur.length);
        System.arraycopy(bytes, 0, next, (int) off, bytes.length);
        parts.put(rel, next);
    }
    public long partSize(String rel) { byte[] b = parts.get(rel); return b == null ? 0 : b.length; }
    public void discardPart(String rel) { parts.remove(rel); }
    public String hashPart(String rel) {
        byte[] b = parts.get(rel);
        return b == null ? "" : SyncCrypto.sha256hex(b);
    }
    public long[] commitPart(String rel, long when) {
        byte[] b = parts.remove(rel);
        disk.put(rel, b == null ? new byte[0] : b);
        return new long[]{ disk.get(rel).length, 2000L };
    }
    public String trash(String rel, long when) { trashed.add(rel); disk.remove(rel); return ".pc-trash/" + rel; }
}

/** The node: content-addressed blobs, and ONE DOCUMENT PER DEVICE keyed exactly as the endpoint
 *  keys them. A sweep that writes to the shared key, or reads only its own, fails here. */
class FakeNet implements SyncIo.Net {
    final Map<String, byte[]> blobs = new LinkedHashMap<String, byte[]>();
    final Map<String, Map<String, Object>> docs = new LinkedHashMap<String, Map<String, Object>>();
    int views = 0, writes = 0;

    public Map<String, Object> views(String folder) {
        views++;
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        Map<String, Object> v = new LinkedHashMap<String, Object>();
        for (Map.Entry<String, Map<String, Object>> e : docs.entrySet()) {
            String[] parts = e.getKey().split(":", 2);
            if (parts.length == 2 && parts[0].equals(folder)) v.put(parts[1], e.getValue());
        }
        out.put("ok", Boolean.TRUE);
        out.put("views", v);
        out.put("unreadable", 0L);
        return out;
    }
    public Map<String, Object> manifest(String folder, Map<String, Object> doc, boolean force, String device) {
        writes++;
        if (doc != null) docs.put(folder + ":" + device, doc);
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        out.put("ok", Boolean.TRUE);
        return out;
    }
    public byte[] getBlob(String sha) throws Exception {
        byte[] b = blobs.get(sha);
        if (b == null) throw new Exception("blob " + sha.substring(0, 8) + " unavailable (404)");
        return b;
    }
    boolean lie = false;                  // answer with an address that is not the bytes' hash
    public String putBlob(byte[] blob) {
        String sha = SyncCrypto.sha256hex(blob);
        blobs.put(sha, blob);
        return lie ? "f".repeat(64) : sha;
    }
    public boolean blobExists(String sha) { return blobs.containsKey(sha); }
}
"""

DRIVER = r"""
package place.poster.app.sync;

import java.util.*;

public class Drv {
  static byte[] body(int i, int size) {
    byte[] b = new byte[size];
    for (int o = 0; o + 4 <= size; o += 4) {
      int v = (i * 2654435761L > 0 ? (int)(i * 2654435761L) : i) ^ o;
      b[o] = (byte)(v >> 24); b[o+1] = (byte)(v >> 16); b[o+2] = (byte)(v >> 8); b[o+3] = (byte)v;
    }
    return b;
  }

  public static void main(String[] a) throws Exception {
    Map<String, Object> out = new LinkedHashMap<String, Object>();
    FakeNet net = new FakeNet();
    byte[] mk = new byte[32];
    for (int i = 0; i < 32; i++) mk[i] = (byte)(i * 7 + 1);
    byte[] secA = new byte[32], secB = new byte[32];
    for (int i = 0; i < 32; i++) { secA[i] = (byte)(i + 3); secB[i] = (byte)(i + 3); }

    // Device A: a folder of small files and one that must be chunked.
    FakeFs fsA = new FakeFs();
    int N = %(files)d;
    for (int i = 0; i < N; i++) fsA.disk.put("DCIM/img" + i + ".jpg", body(i, 600 + (i %% 200)));
    fsA.disk.put("DCIM/clip.mp4", body(999, %(big)d));

    Fake ctxA = new Fake();
    SyncStore stA = new SyncStore(ctxA);
    stA.configure(true, "http://x", "http://x", "wrapped", "laptop-aaa", "[]");
    SyncStore.Folder fA = new SyncStore.Folder();
    fA.key = "Pictures"; fA.id = "tree://a";
    NativeSweep.Report repA = new NativeSweep.Report();
    repA.key = "Pictures";
    NativeSweep.sweep(ctxA, stA, fA, secA, true, null, repA, net, mk, fsA);
    out.put("A_error", repA.error == null ? "" : repA.error);
    out.put("A_unchanged", repA.unchanged);
    out.put("A_uploaded", repA.uploaded.size());
    out.put("A_failed", repA.failed.size());
    out.put("A_trashed", repA.trashed.size());
    out.put("A_docs", new ArrayList<String>(net.docs.keySet()));

    // Device B: nothing on disk, its own journal, reading everything A published.
    FakeFs fsB = new FakeFs();
    Fake ctxB = new Fake();
    SyncStore stB = new SyncStore(ctxB);
    stB.configure(true, "http://x", "http://x", "wrapped", "phone-bbb", "[]");
    SyncStore.Folder fB = new SyncStore.Folder();
    fB.key = "Pictures"; fB.id = "tree://b";
    NativeSweep.Report repB = new NativeSweep.Report();
    repB.key = "Pictures";
    NativeSweep.sweep(ctxB, stB, fB, secB, false, null, repB, net, mk, fsB);
    out.put("B_downloaded", repB.downloaded.size());
    out.put("B_failed", repB.failed.size());
    out.put("B_trashed", repB.trashed.size());

    // Byte for byte.
    int same = 0, differ = 0;
    for (Map.Entry<String, byte[]> e : fsA.disk.entrySet()) {
      byte[] got = fsB.disk.get(e.getKey());
      if (got != null && Arrays.equals(got, e.getValue())) same++; else differ++;
    }
    out.put("same", same);
    out.put("differ", differ);
    out.put("B_files", fsB.disk.size());
    out.put("docs", new ArrayList<String>(net.docs.keySet()));

    // A settled second sweep on B must be quiet: no re-download, nothing trashed.
    NativeSweep.Report repB2 = new NativeSweep.Report();
    repB2.key = "Pictures";
    NativeSweep.sweep(ctxB, stB, fB, secB, false, null, repB2, net, mk, fsB);
    out.put("B2_downloaded", repB2.downloaded.size());
    out.put("B2_uploaded", repB2.uploaded.size());
    out.put("B2_trashed", repB2.trashed.size());
    out.put("B2_unchanged", repB2.unchanged);

    // And a deletion made on A reaches B — through tombstones, not absence.
    fsA.disk.remove("DCIM/img1.jpg");
    NativeSweep.Report repA2 = new NativeSweep.Report();
    repA2.key = "Pictures";
    NativeSweep.sweep(ctxA, stA, fA, secA, false, null, repA2, net, mk, fsA);
    NativeSweep.Report repB3 = new NativeSweep.Report();
    repB3.key = "Pictures";
    NativeSweep.sweep(ctxB, stB, fB, secB, false, null, repB3, net, mk, fsB);
    out.put("A2_tombstoned", repA2.removedRemote.size());
    out.put("B3_trashed", repB3.trashed.size());
    out.put("B3_has_deleted", fsB.disk.containsKey("DCIM/img1.jpg"));

    // A record that goes missing must come back from the journal, without re-uploading a byte.
    net.docs.remove("Pictures:phone-bbb");
    NativeSweep.Report repB4 = new NativeSweep.Report();
    repB4.key = "Pictures";
    NativeSweep.sweep(ctxB, stB, fB, secB, false, null, repB4, net, mk, fsB);
    out.put("B4_uploaded", repB4.uploaded.size());
    out.put("B4_restored", net.docs.containsKey("Pictures:phone-bbb"));

    // A short read must not be stored under a checksum that certifies it.
    {
      FakeFs fsC = new FakeFs();
      fsC.disk.put("DCIM/one.jpg", body(7, 900));
      fsC.shortRead = "DCIM/one.jpg";
      Fake ctxC = new Fake();
      SyncStore stC = new SyncStore(ctxC);
      stC.configure(true, "http://x", "http://x", "wrapped", "short-ccc", "[]");
      SyncStore.Folder fC = new SyncStore.Folder();
      fC.key = "Short"; fC.id = "tree://c";
      NativeSweep.Report repC = new NativeSweep.Report();
      repC.key = "Short";
      NativeSweep.sweep(ctxC, stC, fC, secA, true, null, repC, net, mk, fsC);
      out.put("short_failed", repC.failed.size());
      out.put("short_uploaded", repC.uploaded.size());
    }

    // A store that answers with an address that is not the bytes' hash must be refused.
    {
      FakeFs fsD = new FakeFs();
      fsD.disk.put("DCIM/two.jpg", body(8, 900));
      net.lie = true;
      Fake ctxD = new Fake();
      SyncStore stD = new SyncStore(ctxD);
      stD.configure(true, "http://x", "http://x", "wrapped", "lie-ddd", "[]");
      SyncStore.Folder fD = new SyncStore.Folder();
      fD.key = "Lie"; fD.id = "tree://d";
      NativeSweep.Report repD = new NativeSweep.Report();
      repD.key = "Lie";
      NativeSweep.sweep(ctxD, stD, fD, secA, true, null, repD, net, mk, fsD);
      net.lie = false;
      out.put("lie_failed", repD.failed.size());
      out.put("lie_uploaded", repD.uploaded.size());
    }

    System.out.println(Json.write(out));
  }
}
"""

FAKE = """
/* NOTE: the store here is an INSTANCE field, unlike the copy in test_android_sync_state.py.
 * Two devices are driven in one JVM and they must not share a journal — sharing it made the second
 * device read the first's record of what it had applied, conclude the folder was already in step,
 * and download nothing. */
package place.poster.app.sync;

import android.content.Context;
import android.content.ContentResolver;
import android.content.SharedPreferences;
import java.util.HashMap;
import java.util.Map;

public class Fake extends Context {
  public final Map<String, Object> STORE = new HashMap<String, Object>();
  public ContentResolver getContentResolver() { return null; }
  /* HOW SLOW THE SYSTEM SERVICES ARE, which on a dozing phone is the whole question. Only the two
   * the sweep policy reads are slowed; the alarm service is not, because re-arming is deliberately
   * done on the looper and slowing it would make every path look equally bad. */
  public static volatile long SLOW_MS = 0;
  public Object getSystemService(String name) {
    if (SLOW_MS > 0 && (Context.CONNECTIVITY_SERVICE.equals(name) || Context.BATTERY_SERVICE.equals(name)))
      try { Thread.sleep(SLOW_MS); } catch (InterruptedException ignored) { }
    return null;
  }
  /* ITS OWN DIRECTORY, PER INSTANCE. The journal is a FILE under getFilesDir(), so a shared temp
   * directory makes two devices read each other's record of what they have applied — and makes the
   * record outlive the run, so the second execution of this test starts with the first one's
   * answers. That looked exactly like a first sweep deciding 30 of 31 files needed nothing. */
  private final java.io.File dir = mkdir();
  private static java.io.File mkdir() {
    try {
      java.io.File d = java.io.File.createTempFile("pcdev", "");
      d.delete(); d.mkdirs();
      d.deleteOnExit();
      return d;
    } catch (Exception e) { throw new RuntimeException(e); }
  }
  public java.io.File getFilesDir() { return dir; }
  public SharedPreferences getSharedPreferences(String name, int mode) {
    return new SharedPreferences() {
      public long getLong(String k, long d) { Object v = STORE.get(k); return v == null ? d : (Long) v; }
      public String getString(String k, String d) { Object v = STORE.get(k); return v == null ? d : (String) v; }
      public boolean getBoolean(String k, boolean d) { Object v = STORE.get(k); return v == null ? d : (Boolean) v; }
      public Editor edit() {
        return new Editor() {
          public Editor putLong(String k, long v) { STORE.put(k, Long.valueOf(v)); return this; }
          public Editor putString(String k, String v) { STORE.put(k, v); return this; }
          public Editor putBoolean(String k, boolean v) { STORE.put(k, Boolean.valueOf(v)); return this; }
          public Editor remove(String k) { STORE.remove(k); return this; }
          public void apply() { }
        };
      }
    };
  }
}
"""



def _need(*tools):
    for t in tools:
        if not shutil.which(t):
            pytest.skip("%s is not installed here" % t)


def _run(files=30, big=9 * 1024 * 1024):
    _need("javac", "java")
    with tempfile.TemporaryDirectory() as tmp:
        for name, body in (("World.java", WORLD), ("Fake.java", FAKE),
                           ("Drv.java", DRIVER % {"files": files, "big": big})):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        c = subprocess.run(["javac", "-nowarn", "-d", out, "-sourcepath",
                            STUBS + os.pathsep + os.path.join(ANDROID, "src", "main", "java")]
                           + SRC + [os.path.join(tmp, n) for n in ("World.java", "Fake.java", "Drv.java")],
                           capture_output=True, text=True, timeout=600)
        assert c.returncode == 0, c.stderr[-6000:]
        r = subprocess.run(["java", "-cp", out, "place.poster.app.sync.Drv"],
                           capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, (r.stdout[-2000:] + r.stderr[-6000:])
        return json.loads(r.stdout.strip().splitlines()[-1])


_CACHE = {}


def result():
    if "r" not in _CACHE:
        _CACHE["r"] = _run()
    return _CACHE["r"]


def test_a_native_sweep_uploads_the_folder_and_publishes_its_own_record():
    r = result()
    assert r["A_error"] == "", r
    assert r["A_unchanged"] == 0, "a first sweep found nothing to do: %r" % r
    assert r["A_failed"] == 0, r
    assert r["A_uploaded"] == 31, r          # 30 photos + one file big enough to be chunked
    assert r["A_trashed"] == 0, r
    assert r["A_docs"] == ["Pictures:laptop-aaa"], (
        "the native sweep wrote something other than its OWN device document: %r" % r["A_docs"])


def test_a_second_device_gets_every_byte():
    r = result()
    assert r["B_failed"] == 0, r
    assert r["B_downloaded"] == 31, r
    assert r["B_trashed"] == 0, r
    assert r["differ"] == 0, "%d files came back different" % r["differ"]
    assert r["same"] == 31, r


def test_each_device_publishes_its_own_document_and_only_its_own():
    r = result()
    assert sorted(r["docs"]) == ["Pictures:laptop-aaa", "Pictures:phone-bbb"], r["docs"]


def test_the_next_sweep_is_quiet():
    """A settled folder that re-downloads itself is the loop this design exists to end."""
    r = result()
    assert r["B2_downloaded"] == 0, r
    assert r["B2_uploaded"] == 0, r
    assert r["B2_trashed"] == 0, r
    assert r["B2_unchanged"] == 31, r


def test_a_deletion_travels_as_a_tombstone_and_is_applied_once():
    r = result()
    assert r["A2_tombstoned"] == 1, r
    assert r["B3_trashed"] == 1, r
    assert r["B3_has_deleted"] is False, r


def test_a_record_that_goes_missing_comes_back_from_the_journal():
    """The page half self-heals a lost record; the phone half must too, or its paths stay missing
    from the merge until a file happens to change — and a path nobody claims is a path no joining
    device can fetch."""
    r = result()
    assert r["B4_restored"] is True, "the phone never republished its own record: %r" % r
    assert r["B4_uploaded"] == 0, "it re-uploaded %d files to do it" % r["B4_uploaded"]


def test_a_short_read_is_refused_rather_than_certified():
    """The worse half of the truncation bug: the short buffer is what gets hashed, so the entry's
    checksum certifies the truncation and the receiving device verifies it happily."""
    r = result()
    assert r["short_uploaded"] == 0, "a truncated file was published: %r" % r
    assert r["short_failed"] == 1, r


def test_a_store_that_answers_with_the_wrong_address_is_refused():
    """Content-addressed means the address IS the hash. A different answer is a different file, and
    recording it points the entry at bytes that are not this one."""
    r = result()
    assert r["lie_uploaded"] == 0, "an upload that landed elsewhere was recorded: %r" % r
    assert r["lie_failed"] == 1, r
