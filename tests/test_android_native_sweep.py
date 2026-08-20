"""The Android sweep, RUN — not compiled and read.

This is the half of folder sync that works while the screen is off, and it is the half that moved a
laptop's and a tablet's files into the trash on sweep after sweep while nobody was looking. It could
be compiled, and its reconciler could be held to the JavaScript one decision for decision
(tests/test_android_reconcile_parity.py), and that was all: the sweep itself built its own SAF handle
and its own HTTP client, so nothing could drive it.

Now it takes them (see SyncIo), and this drives a whole one: device A uploads a folder, publishes a
RECORD PER FILE, and device B — a different journal, an empty disk, its own chunk size — reads the
record set, downloads the lot and verifies it byte for byte. Real SyncCrypto (AES-GCM under a real
drive key), real SyncReconcile, real Journal, real chunking. The store is an in-memory relay that
enforces the endpoint's actual rules — a per-file compare-and-swap on the version, an era, and the
tombstone backstop — so a sweep that writes a version the server has already passed is REFUSED here
exactly as it would be in production.

THIS FILE WAS DEAD, and that is the part worth remembering. It was written against the per-device
DOCUMENT engine that the record-set rewrite replaced, so its fakes stopped implementing the
interfaces and every test in it failed at javac — for the whole life of the rewrite, while the sweep
it covers was being changed, and while the bug it would have caught was being reported. A test that
cannot compile is indistinguishable from a test that does not exist, except that it is quieter.
tests/test_android_sync_compiles.py is the floor that stops that happening silently again.

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

/** A folder in memory. Every rule the sweep depends on — part files, trash, hashes, and the
 *  positive proof a deletion claim needs — behaves as the SAF adapter does, because those are what
 *  a resumed or interrupted transfer turns on. */
class FakeFs implements SyncIo.Files {
    final Map<String, byte[]> disk = new LinkedHashMap<String, byte[]>();
    final Map<String, byte[]> parts = new LinkedHashMap<String, byte[]>();
    final List<String> trashed = new ArrayList<String>();
    long mtime = 1000L;
    /** The whole folder is unreachable — the unmounted-drive case, which must confirm NOTHING. */
    boolean blind = false;

    public SafFs.Scan scan(boolean hash, long maxBytes, List<String> excludes) {
        SafFs.Scan s = new SafFs.Scan();
        for (Map.Entry<String, byte[]> e : disk.entrySet()) {
            Map<String, Object> m = new LinkedHashMap<String, Object>();
            m.put("size", (long) e.getValue().length);
            m.put("mtime", mtime);
            // The scan reports the FILE's own hash in `sha`; NativeSweep renames it to
            // `csum`, which is what keeps it from ever being compared to a blob address.
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
    public String hashFile(String rel) {
        byte[] b = disk.get(rel);
        return b == null ? null : SyncCrypto.sha256hex(b);
    }
    public long[] commitPart(String rel, long when) {
        byte[] b = parts.remove(rel);
        disk.put(rel, b == null ? new byte[0] : b);
        return new long[]{ disk.get(rel).length, 2000L };
    }
    public String trash(String rel, long when) { trashed.add(rel); disk.remove(rel); return ".pc-trash/" + rel; }
    /* REMOVE, not "move somewhere else". The trash is one place now and it is on the server, so a
     * deletion here really deletes — reached only after the store has confirmed the bytes. */
    public void remove(String rel) { trashed.add(rel); disk.remove(rel); }
    /** {gone, parentAlive} — the same contract SafFs answers, and the same one the sweep refuses
     *  to publish a deletion without. */
    public boolean[] confirmGone(String rel) {
        if (blind) return new boolean[]{ false, false };
        return new boolean[]{ !disk.containsKey(rel), true };
    }
}

/** The node: content-addressed blobs, and ONE VERSIONED RECORD PER FILE behind a real
 *  compare-and-swap, keyed the way /client/sync-state keys them. A sweep that writes a version the
 *  server has already passed is REFUSED here exactly as it would be in production — which is the
 *  only way the journal-striking path can be driven at all. */
class FakeNet implements SyncIo.Net {
    final Map<String, byte[]> blobs = new LinkedHashMap<String, byte[]>();
    /** pair -> d -> {v, by, ct, t, at, mt} */
    final Map<String, Map<String, Map<String, Object>>> recs =
            new LinkedHashMap<String, Map<String, Map<String, Object>>>();
    final Map<String, Long> eras = new LinkedHashMap<String, Long>();
    long clock = 100000L;
    int refusedBatches = 0;
    static final int TOMB_CAP = 100;      // the server's backstop, mirrored

    Map<String, Map<String, Object>> pair(String p) {
        Map<String, Map<String, Object>> m = recs.get(p);
        if (m == null) { m = new LinkedHashMap<String, Map<String, Object>>(); recs.put(p, m); }
        return m;
    }
    long era(String p) { Long e = eras.get(p); if (e == null) { e = 1L; eras.put(p, e); } return e; }

    public Map<String, Object> state(String pair, Long era, Long since) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        long cur = era(pair);
        boolean full = since == null || era == null || era.longValue() != cur;
        List<Object> rows = new ArrayList<Object>();
        for (Map.Entry<String, Map<String, Object>> e : pair(pair).entrySet()) {
            Map<String, Object> r = e.getValue();
            if (!full && Json.num(r.get("mt"), 0) < since.longValue()) continue;
            Map<String, Object> row = new LinkedHashMap<String, Object>(r);
            row.remove("mt");
            row.put("d", e.getKey());
            rows.add(row);
        }
        out.put("ok", Boolean.TRUE);
        out.put("era", cur);
        out.put("full", Boolean.valueOf(full));
        out.put("records", rows);
        out.put("now", clock);
        return out;
    }

    /** d-tags another device wins the moment this one writes — the CAS race, which cannot be set up
     *  beforehand: a version published in advance is simply one this device then bumps past. */
    final Set<String> raceOnce = new LinkedHashSet<String>();

    public Map<String, Object> putState(String pair, long era, List<Object> put, boolean confirmed) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        if (era != era(pair)) { out.put("ok", Boolean.FALSE); out.put("eraChanged", Boolean.TRUE);
                                return out; }
        for (Object o : put) {
            String d = Json.str(Json.obj(o).get("d"), "");
            if (!raceOnce.remove(d)) continue;
            Map<String, Object> cur = pair(pair).get(d);
            if (cur != null) cur.put("v", Json.num(Json.obj(o).get("v"), 0) + 1);
        }
        int tombs = 0;
        for (Object o : put) if (Json.num(Json.obj(o).get("t"), 0) != 0) tombs++;
        if (tombs > TOMB_CAP && !confirmed) {          // the server's own backstop, refused WHOLE
            refusedBatches++;
            out.put("ok", Boolean.FALSE); out.put("backstop", Boolean.TRUE);
            return out;
        }
        clock++;
        List<Object> results = new ArrayList<Object>();
        Map<String, Map<String, Object>> held = pair(pair);
        for (Object o : put) {
            Map<String, Object> r = Json.obj(o);
            String d = Json.str(r.get("d"), "");
            long v = Json.num(r.get("v"), 0);
            Map<String, Object> cur = held.get(d);
            Map<String, Object> res = new LinkedHashMap<String, Object>();
            res.put("d", d);
            if (cur != null && Json.num(cur.get("v"), 0) >= v) {       // STRICTLY newer, or refused
                res.put("stale", Boolean.TRUE);
                res.put("v", cur.get("v"));
            } else {
                Map<String, Object> row = new LinkedHashMap<String, Object>();
                row.put("v", v);
                row.put("by", r.get("by"));
                row.put("ct", r.get("ct"));
                if (Json.num(r.get("t"), 0) != 0) { row.put("t", 1L); row.put("at", clock); }
                row.put("mt", clock);
                held.put(d, row);
                res.put("stale", Boolean.FALSE);
            }
            results.add(res);
        }
        out.put("ok", Boolean.TRUE);
        out.put("results", results);
        return out;
    }

    /** Publish a record the way ANOTHER device would — used to put the folder into a state this
     *  process did not create (a wave of tombstones from a phone that is not here). */
    void publish(String pair, byte[] mk, String path, Map<String, Object> entry, boolean tomb) {
        Map<String, Object> withPath = new LinkedHashMap<String, Object>();
        withPath.put("path", path);
        withPath.putAll(entry);
        Map<String, Object> row = new LinkedHashMap<String, Object>();
        row.put("v", SyncReconcile.versionOf(entry));
        row.put("by", "elsewhere");
        try {
            row.put("ct", "a1:" + java.util.Base64.getEncoder().encodeToString(
                    SyncCrypto.encrypt(mk, SyncCrypto.utf8(Json.write(withPath)))));
        } catch (Exception e) { throw new RuntimeException(e); }
        if (tomb) { row.put("t", 1L); row.put("at", ++clock); }
        row.put("mt", ++clock);
        pair(pair).put(NativeSweep.pathD(path), row);
    }

    /** What the folder says about one path right now, decrypted — for assertions only. */
    /** Re-seal a record's entry — how a scenario stages a checksum that was published WRONG. */
    void write(String pair, byte[] mk, String path, Map<String, Object> entry) {
        Map<String, Object> row = pair(pair).get(NativeSweep.pathD(path));
        if (row == null) return;
        try {
            Map<String, Object> e = new LinkedHashMap<String, Object>(entry);
            e.put("path", path);
            e.remove("v"); e.remove("t");
            byte[] raw = SyncCrypto.encrypt(mk, SyncCrypto.utf8(Json.write(e)));
            row.put("ct", "a1:" + java.util.Base64.getEncoder().encodeToString(raw));
            row.put("mt", ++clock);
        } catch (Exception ex) { }
    }
    /** What a downloader writes when the store's bytes hash to something else: the failing copy's
     *  address, and — newer clients — the hash it actually measured. */
    void flag(String pair, String path, String value) {
        Map<String, Object> row = pair(pair).get(NativeSweep.pathD(path));
        if (row != null) { row.put("bad", value); row.put("mt", ++clock); }
    }

    Map<String, Object> read(String pair, byte[] mk, String path) {
        Map<String, Object> row = pair(pair).get(NativeSweep.pathD(path));
        if (row == null) return null;
        try {
            String ct = Json.str(row.get("ct"), "");
            byte[] raw = java.util.Base64.getDecoder().decode(ct.substring(3));
            Map<String, Object> e = Json.obj(Json.parse(SyncCrypto.fromUtf8(SyncCrypto.decrypt(mk, raw))));
            e.put("v", row.get("v"));
            if (row.get("t") != null) e.put("t", 1L);
            return e;
        } catch (Exception ex) { return null; }
    }

    public Map<String, Object> views(String folder) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        out.put("ok", Boolean.TRUE);
        out.put("views", new LinkedHashMap<String, Object>());
        out.put("unreadable", 0L);
        return out;
    }
    public Map<String, Object> manifest(String folder, Map<String, Object> doc, boolean force, String device) {
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
        return lie ? "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff" : sha;
    }
    public boolean blobExists(String sha) { return blobs.containsKey(sha); }
    /* THREE ANSWERS, NOT TWO — see SyncIo.Net.hasBlob. `unreachable` is what a scenario sets to say
     * the store could not be asked at all, which must never be read as "the bytes are gone". */
    boolean unreachable = false;
    public Boolean hasBlob(String sha) {
        if (unreachable) return null;
        return blobs.containsKey(sha) ? Boolean.TRUE : Boolean.FALSE;
    }
}
"""

DRIVER = r"""
package place.poster.app.sync;

import java.util.*;

public class Drv {
  static byte[] body(int i, int size) {
    byte[] b = new byte[size];
    for (int o = 0; o + 4 <= size; o += 4) {
      int v = (int) (i * 2654435761L) ^ o;
      b[o] = (byte)(v >> 24); b[o+1] = (byte)(v >> 16); b[o+2] = (byte)(v >> 8); b[o+3] = (byte)v;
    }
    return b;
  }
  static SyncStore store(Fake ctx, String device) {
    SyncStore st = new SyncStore(ctx);
    st.configure(true, "http://x", "http://x", "wrapped", device, "[]");
    return st;
  }
  static SyncStore.Folder folder(String key, String id) {
    SyncStore.Folder f = new SyncStore.Folder();
    f.key = key; f.id = id;
    return f;
  }
  static NativeSweep.Report sweep(Fake ctx, SyncStore st, SyncStore.Folder f, byte[] sec,
                                  boolean hash, FakeNet net, byte[] mk, FakeFs fs) throws Exception {
    NativeSweep.Report rep = new NativeSweep.Report();
    rep.key = f.key;
    NativeSweep.sweep(ctx, st, f, sec, hash, null, rep, net, mk, fs);
    return rep;
  }

  public static void main(String[] a) throws Exception {
    Map<String, Object> out = new LinkedHashMap<String, Object>();
    FakeNet net = new FakeNet();
    byte[] mk = new byte[32];
    for (int i = 0; i < 32; i++) mk[i] = (byte)(i * 7 + 1);
    byte[] sec = new byte[32];
    for (int i = 0; i < 32; i++) sec[i] = (byte)(i + 3);

    // ---- device A: a folder of small files and one big enough to be chunked.
    FakeFs fsA = new FakeFs();
    int N = %(files)d;
    for (int i = 0; i < N; i++) fsA.disk.put("DCIM/img" + i + ".jpg", body(i, 600 + (i %% 200)));
    fsA.disk.put("DCIM/clip.mp4", body(999, %(big)d));

    Fake ctxA = new Fake();
    SyncStore stA = store(ctxA, "laptop-aaa");
    SyncStore.Folder fA = folder("Pictures", "tree://a");
    NativeSweep.Report repA = sweep(ctxA, stA, fA, sec, true, net, mk, fsA);
    out.put("A_error", repA.error == null ? "" : repA.error);
    out.put("A_unchanged", repA.unchanged);
    out.put("A_uploaded", repA.uploaded.size());
    out.put("A_failed", repA.failed.size());
    out.put("A_trashed", repA.trashed.size());
    out.put("A_records", net.pair("Pictures").size());

    // ---- device B: nothing on disk, its own journal, reading the record set.
    FakeFs fsB = new FakeFs();
    Fake ctxB = new Fake();
    SyncStore stB = store(ctxB, "phone-bbb");
    SyncStore.Folder fB = folder("Pictures", "tree://b");
    NativeSweep.Report repB = sweep(ctxB, stB, fB, sec, false, net, mk, fsB);
    out.put("B_downloaded", repB.downloaded.size());
    out.put("B_failed", repB.failed.size());
    out.put("B_trashed", repB.trashed.size());

    int same = 0, differ = 0;
    for (Map.Entry<String, byte[]> e : fsA.disk.entrySet()) {
      byte[] got = fsB.disk.get(e.getKey());
      if (got != null && Arrays.equals(got, e.getValue())) same++; else differ++;
    }
    out.put("same", same);
    out.put("differ", differ);
    out.put("B_files", fsB.disk.size());
    out.put("records", net.pair("Pictures").size());

    // ---- a settled second sweep must be quiet.
    NativeSweep.Report repB2 = sweep(ctxB, stB, fB, sec, false, net, mk, fsB);
    out.put("B2_downloaded", repB2.downloaded.size());
    out.put("B2_uploaded", repB2.uploaded.size());
    out.put("B2_trashed", repB2.trashed.size());
    out.put("B2_unchanged", repB2.unchanged);

    /* ---- A REWRITE IS NOT AN EDIT. Every file keeps its bytes and gets a new timestamp, which is
     * what an rsync, a restore from backup or a second sync engine on the same directory leaves
     * behind. The plan is built from a stamp, so all 31 read as "changed here" — and re-uploading a
     * folder that is already stored, over a radio, is the whole cost this rule removes. */
    Object vWas = net.read("Pictures", mk, "DCIM/img1.jpg").get("v");
    fsB.mtime = 55555L;
    NativeSweep.Report repB2r = sweep(ctxB, stB, fB, sec, false, net, mk, fsB);
    out.put("B2r_uploaded", repB2r.uploaded.size());
    out.put("B2r_settled", repB2r.settledByContent);
    out.put("B2r_failed", repB2r.failed.size());
    out.put("B2r_version_moved", !String.valueOf(vWas)
            .equals(String.valueOf(net.read("Pictures", mk, "DCIM/img1.jpg").get("v"))));
    /* And it has to STAY quiet: the settle must write the new stamp into the journal, or the next
     * sweep hashes all 31 again, for ever. */
    NativeSweep.Report repB2s = sweep(ctxB, stB, fB, sec, false, net, mk, fsB);
    out.put("B2s_settled", repB2s.settledByContent);
    out.put("B2s_unchanged", repB2s.unchanged);


    // ---- ONE deletion on A travels as a tombstone, keeps its address, and B applies it once.
    fsA.disk.remove("DCIM/img1.jpg");
    NativeSweep.Report repA2 = sweep(ctxA, stA, fA, sec, false, net, mk, fsA);
    out.put("A2_tombstoned", repA2.removedRemote.size());
    Map<String, Object> tomb = net.read("Pictures", mk, "DCIM/img1.jpg");
    out.put("A2_tomb_has_sha", tomb != null && tomb.get("sha") != null);
    out.put("A2_tomb_has_csum", tomb != null && tomb.get("csum") != null);
    NativeSweep.Report repB3 = sweep(ctxB, stB, fB, sec, false, net, mk, fsB);
    out.put("B3_trashed", repB3.trashed.size());
    out.put("B3_has_deleted", fsB.disk.containsKey("DCIM/img1.jpg"));

    // ---- A TOMBSTONE TAKES ITS ADDRESS FROM WHICHEVER SIDE STILL HAS ONE.
    // A journal entry that lost its address (a struck CAS write, an era change, an older build)
    // must not shadow a shared record that still has one: an address-less tombstone cannot be
    // restored account-wide, and no device holding the file can ever settle against it.
    {
      Map<String, Map<String, Object>> j = stA.base("Pictures");
      Map<String, Object> bare = new LinkedHashMap<String, Object>();
      bare.put("v", Json.num(j.get("DCIM/img2.jpg").get("v"), 0));
      bare.put("by", "laptop-aaa");
      Map<String, Object> loc = new LinkedHashMap<String, Object>();
      loc.put("size", (long) fsA.disk.get("DCIM/img2.jpg").length);
      loc.put("mtime", 1000L);
      bare.put("local", loc);
      j.put("DCIM/img2.jpg", bare);           // no sha, no csum — all it remembers is the version
      stA.saveBase("Pictures", j);
      fsA.disk.remove("DCIM/img2.jpg");
      NativeSweep.Report repA3 = sweep(ctxA, stA, fA, sec, false, net, mk, fsA);
      Map<String, Object> t2 = net.read("Pictures", mk, "DCIM/img2.jpg");
      out.put("bare_tombstoned", repA3.removedRemote.size());
      out.put("bare_tomb_has_sha", t2 != null && t2.get("sha") != null);
      out.put("bare_tomb_has_csum", t2 != null && t2.get("csum") != null);
    }

    /* ---- A WAVE OF DELETIONS IS APPLIED, BECAUSE EVERY ONE OF THEM WAS CHECKED.
     *
     * This used to be refused by an absolute floor, and the floor was the wrong instrument: 25
     * tombstones against 300 survivors passes every proportional guard, so the floor was all that
     * spoke — and it spoke on BOTH sides, refusing the device that still held the only copies while
     * merely asking the ones with nothing to lose.
     *
     * A background sweep carries no person to ask, which is precisely why the question must not be
     * a matter of opinion. It asks the STORE instead, once per file: only bytes it can give back
     * are deleted from here. Published the way another device would publish them. */
    {
      FakeFs fsE = new FakeFs();
      Fake ctxE = new Fake();
      SyncStore stE = store(ctxE, "tablet-eee");
      SyncStore.Folder fE = folder("Wave", "tree://e");
      for (int i = 0; i < 300; i++) fsE.disk.put("keep/f" + i + ".txt", body(i, 40));
      for (int i = 0; i < 25; i++) fsE.disk.put("important/k" + i + ".docx", body(5000 + i, 60));
      NativeSweep.Report r1 = sweep(ctxE, stE, fE, sec, true, net, mk, fsE);
      out.put("wave_uploaded", r1.uploaded.size());
      // Another device deletes those 25 — 25 tombstones against 300 survivors: every ratio says fine.
      for (int i = 0; i < 25; i++) {
        String path = "important/k" + i + ".docx";
        Map<String, Object> live = net.read("Wave", mk, path);
        Map<String, Object> t = new LinkedHashMap<String, Object>();
        t.put("v", Json.num(live.get("v"), 0) + 1);
        t.put("deletedAt", 5000L);
        t.put("sha", live.get("sha"));
        t.put("csum", live.get("csum"));
        net.publish("Wave", mk, path, t, true);
      }
      /* A SECOND DEVICE IN THE SAME SITUATION, captured BEFORE the deletions run — same folder,
       * same 25 tombstones, but its store cannot be reached. */
      FakeFs fsU = new FakeFs();
      fsU.disk.putAll(fsE.disk);
      NativeSweep.Report r2 = sweep(ctxE, stE, fE, sec, false, net, mk, fsE);
      out.put("wave_trashed", r2.trashed.size());
      out.put("wave_refused", r2.refusedTrash);
      out.put("wave_files_left", fsE.disk.size());
      /* …AND THE SAME WAVE WITH THE STORE UNREACHABLE CHANGES NOTHING ON DISK. Not one file, and
       * not silently: a deletion that did not happen looks exactly like one that did. */
      Fake ctxU = new Fake();
      SyncStore stU = store(ctxU, "tablet-uuu");
      SyncStore.Folder fU = folder("Wave", "tree://u");
      NativeSweep.Report r3;
      net.unreachable = true;
      try { r3 = sweep(ctxU, stU, fU, sec, true, net, mk, fsU); }
      finally { net.unreachable = false; }
      out.put("wave_offline_trashed", r3.trashed.size());
      out.put("wave_offline_kept", r3.keptUnconfirmed.size());
    }

    // ---- …AND BELOW THE FLOOR AN ORDINARY DELETION STILL JUST HAPPENS. A guard that stops
    // everything is the same bug with its sign flipped. Its own pair, because the refused wave
    // above is still pending on that one and would be counted with it — which is correct
    // behaviour and the wrong measurement.
    {
      FakeFs fsF = new FakeFs();
      Fake ctxF = new Fake();
      SyncStore stF = store(ctxF, "few-fff");
      SyncStore.Folder fF = folder("Few", "tree://f");
      for (int i = 0; i < 300; i++) fsF.disk.put("keep/f" + i + ".txt", body(i, 40));
      sweep(ctxF, stF, fF, sec, true, net, mk, fsF);
      for (int i = 0; i < 3; i++) {
        String path = "keep/f" + i + ".txt";
        Map<String, Object> live = net.read("Few", mk, path);
        Map<String, Object> t = new LinkedHashMap<String, Object>();
        t.put("v", Json.num(live.get("v"), 0) + 1);
        t.put("deletedAt", 5000L);
        t.put("sha", live.get("sha"));
        t.put("csum", live.get("csum"));
        net.publish("Few", mk, path, t, true);
      }
      NativeSweep.Report rF = sweep(ctxF, stF, fF, sec, false, net, mk, fsF);
      out.put("few_trashed", rF.trashed.size());
      out.put("few_refused", rF.refusedTrash);
    }

    // ---- AND POINTING OUTWARDS: a folder this device can no longer read deletes nothing anywhere.
    {
      FakeFs fsG = new FakeFs();
      Fake ctxG = new Fake();
      SyncStore stG = store(ctxG, "gone-ggg");
      SyncStore.Folder fG = folder("Blind", "tree://g");
      // Ten, deliberately: past FLOOR the mass-tombstone guard answers first and this would be
      // measuring that instead of the per-path proof, which is the thing that has to hold at ANY
      // size — one file lost to a drive hiccup is still a file deleted off every device.
      for (int i = 0; i < 10; i++) fsG.disk.put("d/f" + i + ".txt", body(i, 30));
      sweep(ctxG, stG, fG, sec, true, net, mk, fsG);
      fsG.disk.clear();                       // the drive went away; the listing is empty
      fsG.blind = true;                       // …and the probe cannot confirm anything
      NativeSweep.Report rG = sweep(ctxG, stG, fG, sec, false, net, mk, fsG);
      out.put("blind_removed", rG.removedRemote.size());
      out.put("blind_held", rG.unconfirmedAbsent.size());
      int live = 0;
      for (Map<String, Object> r : net.pair("Blind").values()) if (r.get("t") == null) live++;
      out.put("blind_still_live", live);
    }

    // ---- a short read must not be stored under a checksum that certifies it.
    {
      FakeFs fsC = new FakeFs();
      fsC.disk.put("DCIM/one.jpg", body(7, 900));
      fsC.shortRead = "DCIM/one.jpg";
      Fake ctxC = new Fake();
      NativeSweep.Report repC = sweep(ctxC, store(ctxC, "short-ccc"), folder("Short", "tree://c"),
                                      sec, true, net, mk, fsC);
      out.put("short_failed", repC.failed.size());
      out.put("short_uploaded", repC.uploaded.size());
    }

    // ---- a store that answers with an address that is not the bytes' hash must be refused.
    {
      FakeFs fsD = new FakeFs();
      fsD.disk.put("DCIM/two.jpg", body(8, 900));
      net.lie = true;
      Fake ctxD = new Fake();
      NativeSweep.Report repD = sweep(ctxD, store(ctxD, "lie-ddd"), folder("Lie", "tree://d"),
                                      sec, true, net, mk, fsD);
      net.lie = false;
      out.put("lie_failed", repD.failed.size());
      out.put("lie_uploaded", repD.uploaded.size());
    }

    // ---- the CAS loser is struck from the journal, not silently believed.
    {
      FakeFs fsH = new FakeFs();
      Fake ctxH = new Fake();
      SyncStore stH = store(ctxH, "race-hhh");
      SyncStore.Folder fH = folder("Race", "tree://h");
      fsH.disk.put("r/one.txt", body(1, 50));
      sweep(ctxH, stH, fH, sec, true, net, mk, fsH);
      // Another device wins that file's next version at the exact moment this one writes it.
      net.raceOnce.add(NativeSweep.pathD("r/one.txt"));
      fsH.disk.put("r/one.txt", body(2, 70));      // edited here
      fsH.mtime = 9999L;
      NativeSweep.Report rH = sweep(ctxH, stH, fH, sec, true, net, mk, fsH);
      out.put("race_journal_kept", stH.base("Race").containsKey("r/one.txt"));
      out.put("race_record_v", Json.num(net.read("Race", mk, "r/one.txt").get("v"), 0));
      out.put("race_failed", rH.failed.size());
    }


    /* ---- AND A REPUBLISHED RECORD IS NOT FETCHED BACK OVER BYTES THIS PHONE ALREADY HOLDS.
     * The planner compares content only when both sides carry a checksum, and the scan does not
     * hash unless it has to — so a record republished with the uploader's own timestamp looks like a
     * different file, and every device fetches what it is already holding. On a phone that is the
     * whole folder over a radio, on battery. */
    for (String p : new ArrayList<String>(net.pair("Pictures").keySet())) {
      Map<String, Object> rec = net.pair("Pictures").get(p);
      if (Json.num(rec.get("t"), 0) != 0) continue;
      rec.put("v", Json.num(rec.get("v"), 1) + 1);          // republished, same bytes, new version
    }
    NativeSweep.Report repB4 = sweep(ctxB, stB, fB, sec, false, net, mk, fsB);
    out.put("B4_downloaded", repB4.downloaded.size());
    out.put("B4_held", repB4.heldAlready);

    /* These run LAST: they deliberately publish a wrong checksum, and a folder in
     * that state is not what the counts above are about. */
    /* ---- A FLAGGED RECORD IS REPAIRED BY THE BACKGROUND SWEEP, not only by opening the app.
     *
     * The situation this exists for, exactly as it happened: this app's own digest stopped at a
     * zero-length read and published the hash of a PREFIX. Every other device then downloaded the
     * file correctly, hashed it, disagreed with the record, and refused it — for ever, since the
     * only device able to repair it is the one that uploaded it, and its background sweep did not
     * even carry the flag out of the envelope. Measured on one multi-gigabyte .jex: sixteen
     * download rounds in ninety minutes, 1.14 GB re-fetched, no possible end.
     *
     * Note which way the repair has to go. After the digest is fixed the holder hashes its own
     * perfectly good file and STILL disagrees with what it published — so the naive rule ("my file
     * does not match what I published, so my file is bad") makes the symptom permanent. What
     * decides it is that the downloader's measured hash and ours are the same: two independent
     * readings agree about the content, and the checksum is the odd one out. */
    {
      String victim = "DCIM/img7.jpg";
      String truth = SyncCrypto.sha256hex(fsA.disk.get(victim));
      Map<String, Object> rec = net.read("Pictures", mk, victim);
      String addr = SyncDiff.addressOf(rec);
      out.put("H_truth_matches_record", truth.equals(Json.str(rec.get("csum"), "")));
      // Publish a WRONG checksum, as the broken digest did, and flag it the way a downloader does.
      Map<String, Object> broken = new LinkedHashMap<String, Object>(rec);
      broken.put("csum", "00" + truth.substring(2));
      net.write("Pictures", mk, victim, broken);
      net.flag("Pictures", victim, addr + "|" + truth);
      NativeSweep.Report repH = sweep(ctxA, stA, fA, sec, false, net, mk, fsA);
      out.put("H_reseeded", repH.reseeding.size());
      out.put("H_stale", repH.staleChecksum.size());
      out.put("H_badhere", repH.badHere.size());
      out.put("H_uploaded", repH.uploaded.size());
      Map<String, Object> after = net.read("Pictures", mk, victim);
      out.put("H_csum_now_true", truth.equals(Json.str(after.get("csum"), "")));
      /* THE ADDRESS DOES NOT CHANGE HERE, AND IT MUST NOT BE ASKED TO. The seal is convergent —
       * the IV is derived from the content — so identical bytes always land at the same address,
       * which is what makes the blob store dedupe. In this repair the bytes were never the problem,
       * so there are no new bytes and no new address. What lifts every other device's refusal is
       * the VERSION moving past the one their failure was recorded at. */
      out.put("H_version_moved", Json.num(after.get("v"), 0) > Json.num(rec.get("v"), 0));
      out.put("H_flag_cleared", net.pair("Pictures").get(NativeSweep.pathD(victim)).get("bad") == null);
      // …and a second sweep does not do it again: the flag names a copy that no longer exists.
      NativeSweep.Report repH2 = sweep(ctxA, stA, fA, sec, false, net, mk, fsA);
      out.put("H2_reseeded", repH2.reseeding.size());
      out.put("H2_uploaded", repH2.uploaded.size());
    }

    /* ---- AND A DEVICE WHOSE OWN COPY REALLY IS DIFFERENT RE-SEEDS NOTHING. Two readings that
     * disagree with the checksum AND with each other say nothing about the content; spreading this
     * copy would be spreading damage. */
    {
      String victim = "DCIM/img8.jpg";
      Map<String, Object> rec = net.read("Pictures", mk, victim);
      // The record says one thing, the downloader measured another, and OUR copy is a third: three
      // readings, no two of which agree. Nothing here is evidence about the content.
      Map<String, Object> odd = new LinkedHashMap<String, Object>(rec);
      odd.put("csum", SyncCrypto.sha256hex(body(4241, 700)));
      net.write("Pictures", mk, victim, odd);
      net.flag("Pictures", victim, SyncDiff.addressOf(rec) + "|" + SyncCrypto.sha256hex(body(4242, 700)));
      NativeSweep.Report repX = sweep(ctxA, stA, fA, sec, false, net, mk, fsA);
      out.put("X_reseeded", repX.reseeding.size());
      out.put("X_badhere", repX.badHere.size());
      out.put("X_uploaded", repX.uploaded.size());
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


def test_a_native_sweep_uploads_the_folder_and_publishes_a_record_per_file():
    r = result()
    assert r["A_error"] == "", r
    assert r["A_unchanged"] == 0, "a first sweep found nothing to do: %r" % r
    assert r["A_failed"] == 0, r
    assert r["A_uploaded"] == 31, r          # 30 photos + one file big enough to be chunked
    assert r["A_trashed"] == 0, r
    assert r["A_records"] == 31, (
        "one record per file is the whole design; the sweep published %r" % r["A_records"])


def test_a_second_device_gets_every_byte():
    r = result()
    assert r["B_failed"] == 0, r
    assert r["B_downloaded"] == 31, r
    assert r["B_trashed"] == 0, r
    assert r["differ"] == 0, "%d files came back different" % r["differ"]
    assert r["same"] == 31, r


def test_the_second_device_adds_no_records_of_its_own():
    """A device that re-publishes what it just downloaded is the loop per-file records exist to end
    — and every republish costs every OTHER device a version bump to re-examine."""
    r = result()
    assert r["records"] == 31, r


def test_the_next_sweep_is_quiet():
    """A settled folder that re-downloads itself is the other half of that loop."""
    r = result()
    assert r["B2_downloaded"] == 0, r
    assert r["B2_uploaded"] == 0, r
    assert r["B2_trashed"] == 0, r
    assert r["B2_unchanged"] == 31, r


def test_a_rewrite_that_changed_no_byte_uploads_nothing():
    """Reported from a desktop whose counts were all in agreement — 11,939 here, 11,939 in the
    folder, 11,939 in the store — as "why is desktop uploading 2/19 files right now! sync was
    finished!". Nothing was wrong: the plan is built from a stamp, so anything that rewrites a file
    without changing a byte reads as an edit. On a phone that is a folder re-sent over a radio."""
    r = result()
    assert r["B2r_uploaded"] == 0, r
    assert r["B2r_settled"] == 31, r
    assert r["B2r_failed"] == 0, r


def test_and_it_publishes_no_new_version():
    """This device learned nothing the folder did not already know. A bump would hand every other
    device 31 files to re-examine because one machine ran an rsync."""
    r = result()
    assert r["B2r_version_moved"] is False, r


def test_and_the_sweep_after_it_is_quiet_again():
    """The settle must write the new stamp into the journal. Without that the folder is hashed in
    full on every sweep for ever — cheaper than uploading, and still wrong."""
    r = result()
    assert r["B2s_settled"] == 0, r
    assert r["B2s_unchanged"] == 31, r


def test_a_republished_record_is_not_fetched_back_over_bytes_we_hold():
    """Measured on a desktop restored from a NAS backup: 223 blobs downloaded in twelve minutes,
    every one a file it already held byte-for-byte, while its real uploads sat at eleven in half an
    hour. On a phone the same waste is the whole folder over a radio, on battery."""
    r = result()
    assert r["B4_downloaded"] == 0, r
    # 29, not the original 31: the deletion and conflict steps above account for the other two. The
    # load-bearing number is the zero — every live file this phone holds was settled against its own
    # bytes instead of fetched.
    assert r["B4_held"] == 29, r


def test_a_deletion_travels_as_a_tombstone_and_is_applied_once():
    r = result()
    assert r["A2_tombstoned"] == 1, r
    assert r["B3_trashed"] == 1, r
    assert r["B3_has_deleted"] is False, r


def test_a_tombstone_carries_the_address_of_what_it_deleted():
    """Without it there is nothing to restore: the bytes are still in the store and nothing
    remembers which bytes they were."""
    r = result()
    assert r["A2_tomb_has_sha"] is True, r
    assert r["A2_tomb_has_csum"] is True, r


def test_a_tombstone_takes_the_address_from_whichever_side_still_has_one():
    """`index[p]` alone was what this read, and a journal entry that had lost its address SHADOWED a
    shared record that still had one. The result is an address-less tombstone, which breaks two
    things at once and says neither: "Deleted on every device" cannot offer the file, and no device
    still holding it can ever settle against the deletion — delete-loses-to-edit compares csums, an
    absent csum always reads as an edit, so it republishes for ever and trips the resurrect floor
    for ever. That is the standoff that was reported as "it always wants to republish"."""
    r = result()
    assert r["bare_tombstoned"] == 1, r
    assert r["bare_tomb_has_sha"] is True, (
        "the phone published a tombstone naming no bytes — nothing can undo it: %r" % r)
    assert r["bare_tomb_has_csum"] is True, r


def test_a_wave_of_deletions_is_applied_because_every_one_was_checked():
    """THE REPORTED FAILURE, AND WHY THE FLOOR WAS THE WRONG INSTRUMENT.

    25 stale tombstones against 300 surviving files passes every proportional guard there is, so an
    absolute floor was all that spoke — and it spoke on both sides, refusing the device that still
    held the only copies while merely asking the ones with nothing to lose. A background sweep
    carries no person to ask, which is exactly why the question must not be a matter of opinion.

    It asks the store instead: these bytes are there, so the deletion can be undone from the one
    trash on the server, so it applies.
    """
    r = result()
    assert r["wave_uploaded"] == 325, r
    assert r["wave_trashed"] == 25, "it applied %r of 25 verified deletions" % r["wave_trashed"]
    assert not r["wave_refused"], "a count-based guard came back: %r" % r["wave_refused"]
    assert r["wave_files_left"] == 300, r


def test_and_the_same_wave_deletes_nothing_when_the_store_cannot_be_asked():
    """The whole safety of an automatic deletion is that the bytes were confirmed recoverable
    first. No answer is not an answer — a rate limiter, a dead socket, a phone that has just lost
    its network is the most ordinary thing in the world on the device this runs on."""
    r = result()
    assert r["wave_offline_trashed"] == 0, (
        "it deleted %r files it could not verify were recoverable" % r["wave_offline_trashed"])
    assert r["wave_offline_kept"] == 25, (
        "it kept the files but reported %r of them — a deletion that did not happen looks exactly "
        "like one that did unless somebody says so" % r["wave_offline_kept"])


def test_and_an_ordinary_deletion_still_just_happens():
    """The guard that stops everything is the same bug with its sign flipped — it is what stopped
    the contacts sweep syncing at all. Below the floor, nothing is questioned."""
    r = result()
    assert r["few_trashed"] == 3, r
    assert r["few_refused"] == "", r


def test_a_folder_this_device_cannot_read_deletes_nothing_anywhere():
    """The unmounted-drive case, pointing outwards. An empty listing is not an empty folder, and
    without positive proof per path this used to publish a deletion for every file it knew."""
    r = result()
    assert r["blind_removed"] == 0, r
    assert r["blind_held"] == 10, r
    assert r["blind_still_live"] == 10, "%r records were tombstoned by a drive going away" % r


def test_a_short_read_is_refused_rather_than_certified():
    """The worse half of the truncation bug: the short buffer is what gets hashed, so the record's
    checksum certifies the truncation and the receiving device verifies it happily."""
    r = result()
    assert r["short_uploaded"] == 0, "a truncated file was published: %r" % r
    assert r["short_failed"] == 1, r


def test_a_store_that_answers_with_the_wrong_address_is_refused():
    """Content-addressed means the address IS the hash. A different answer is a different file, and
    recording it points the record at bytes that are not this one."""
    r = result()
    assert r["lie_uploaded"] == 0, "an upload that landed elsewhere was recorded: %r" % r
    assert r["lie_failed"] == 1, r


def test_the_loser_of_a_compare_and_swap_is_struck_from_the_journal():
    """The refusal is the whole safety of concurrent devices: the loser must end up knowing NOTHING
    about that path, so the next sweep resolves the divergence as a conflict with both copies
    surviving. A device that kept its journal entry would instead believe in an agreement the server
    rejected, and quietly stop offering its own copy."""
    r = result()
    assert r["race_journal_kept"] is False, (
        "the phone kept believing a write the server refused: %r" % r)
    assert r["race_failed"] == 0, "a lost CAS is not a failure — it resolves next sweep: %r" % r


def test_the_background_sweep_repairs_a_flagged_record():
    """THE .jex LOOP, ENDED AT ITS SOURCE.

    This app's own digest stopped at a zero-length read and published the hash of a PREFIX. Every
    other device downloaded the file correctly, hashed it, disagreed with the record and refused
    it — for ever, because the only device that could repair it is the one that uploaded it, and
    the background sweep did not even carry the flag out of the envelope. Measured on one
    multi-gigabyte file: sixteen download rounds in ninety minutes, 1.14 GB re-fetched, with no
    possible end.
    """
    r = result()
    assert r["H_truth_matches_record"] is True, (
        "the scenario did not start from a correctly published file: %r" % r)
    assert r["H_reseeded"] == 1, "the flagged record was not repaired: %r" % r
    assert r["H_uploaded"] == 1, "it reported a repair without sending anything: %r" % r
    assert r["H_csum_now_true"] is True, (
        "it re-sent without correcting the checksum, so every device still refuses it: %r" % r)
    assert r["H_version_moved"] is True, (
        "the record's version did not move, so no other device's refusal can lift — and the seal "
        "is convergent, so identical bytes cannot supply a new address to lift it instead: %r" % r)
    assert r["H_flag_cleared"] is True, (
        "the flag survived the repair, so the next sweep repairs it again, for ever: %r" % r)


def test_it_is_the_checksum_that_was_wrong_not_the_file():
    """The direction matters. After the digest is fixed the holder hashes its own perfectly good
    file and STILL disagrees with what it published — so "my file does not match what I published,
    so my file is bad" makes the symptom permanent. What decides it is that the downloader's
    measured hash and ours agree: two independent readings of the content against one recorded
    number, and the number is the odd one out."""
    r = result()
    assert r["H_stale"] == 1, "it did not recognise its own published checksum as the wrong one: %r" % r
    assert r["H_badhere"] == 0, "it condemned its own good copy: %r" % r


def test_a_repaired_record_is_not_repaired_again_every_sweep():
    """The flag names the copy that failed. Once a different copy is stored the flag has been
    answered, and repairing again is the loop the flag exists to end."""
    r = result()
    assert r["H2_reseeded"] == 0, r
    assert r["H2_uploaded"] == 0, r


def test_a_device_whose_own_copy_really_is_different_re_seeds_nothing():
    """Two readings that disagree with the checksum AND with each other say nothing about the
    content. Spreading this copy would be spreading damage."""
    r = result()
    assert r["X_reseeded"] == 0, "it re-seeded a copy nothing agrees with: %r" % r
    assert r["X_uploaded"] == 0, r
    assert r["X_badhere"] == 1, "it said nothing about a copy it refused to send: %r" % r
