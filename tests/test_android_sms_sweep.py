"""The launcher's Texts app archives, and this RUNS the code that makes it.

The handset had two ways into the Nostr archive and only one of them could back anything up: a
live-arrival sealer for single texts, and `sms.js:mirror()` — JavaScript, inside the client's
WebView, running only while somebody had PosterChan → Texts open on screen. The launcher's Texts
app is native, so opening it archived nothing, ever. "Should not have to open PosterChan → Texts
when we have an android launcher app called Texts" is the report, and it was exactly right.

SmsSweep is that half in Java. This file drives a whole one against a fake provider and a fake
blob store: real doc addressing (SmsKeys, the same function the JavaScript is held to in
tests/test_android_sms.py), real body shape, real attachment paging.

Every test here is a failure this feature has ALREADY HAD, in one language or the other:

  * a refused attachment froze the high-water mark at its row, and since the provider answers
    oldest-first, ten refusals at the old end stood in front of every newer message — 213 rows
    read, `published: 0`, the mark unchanged sweep after sweep;
  * a chunk cap was read as a file size, so every camera photo archived as a picture message
    carrying no picture (1,284 of one account's 1,964 documents);
  * a sweep that advanced its own mark with no relay connected would throw the window away.

What it does NOT cover, because only a phone can: the real ContentProvider, Keystore, and the
relay socket. Those are named so nobody mistakes this for having run on a handset.
"""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
SMS = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app", "sms")
STUBS = os.path.join(ROOT, "tests", "androidstubs")

SYNC = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app", "sync")

SRC = ([os.path.join(SMS, f + ".java") for f in ("SmsSweep", "SmsMsg", "SmsPart", "SmsKeys")]
       + [os.path.join(SYNC, "Json.java")])

DRIVER = r"""
package place.poster.app.sms;

import org.json.JSONObject;
import place.poster.app.sync.Json;
import java.util.*;

/** A phone whose provider, drive and signer are all HashMaps. */
class Drv {

  /* A provider that can refuse, can be too large for one read, and can lie about nothing else. */
  static class Store implements SmsSweep.Parts {
    final Map<Long, byte[]> bytes = new HashMap<Long, byte[]>();
    final Set<Long> refuse = new HashSet<Long>();
    final Set<Long> noSize = new HashSet<Long>();
    int wholeReads = 0;
    public byte[] bytes(long id, int max) {
      if (refuse.contains(id)) return null;
      byte[] b = bytes.get(id);
      if (b == null) return null;
      return b.length > max ? null : b;          // A CAP, NOT A MEASUREMENT.
    }
    public byte[] chunk(long id, long off, int max) {
      if (refuse.contains(id)) return null;
      byte[] b = bytes.get(id);
      if (b == null || off >= b.length) return null;
      wholeReads++;
      int n = (int) Math.min(max, b.length - off);
      byte[] out = new byte[n];
      System.arraycopy(b, (int) off, out, 0, n);
      return out;
    }
    public long size(long id) {
      if (noSize.contains(id)) return -1;
      byte[] b = bytes.get(id);
      return b == null ? -1 : b.length;
    }
  }

  static class World implements SmsSweep.Io {
    final List<SmsMsg> rows = new ArrayList<SmsMsg>();
    final Store store = new Store();
    final Map<String, byte[]> blobs = new LinkedHashMap<String, byte[]>();
    long mark = 0;
    boolean sealThrows = false;
    int sealed = 0;
    /* WHAT WAS SIGNED, IN PLAIN JAVA. `tests/androidstubs/org/json` is a compile-only shell whose
     * put() discards and whose toString() answers "{}" — reading a document back through it would
     * make every assertion below agree with an empty message. The sweep builds its body with the
     * repo's own Json writer (a real one), so the fake signer keeps that text verbatim. */
    final List<String> docs = new ArrayList<String>();
    final List<String> bodies = new ArrayList<String>();

    public List<SmsMsg> since(long dateMs, int limit) {
      List<SmsMsg> out = new ArrayList<SmsMsg>();
      for (SmsMsg m : rows) if (m.date > dateMs) out.add(m);
      Collections.sort(out, new Comparator<SmsMsg>() {
        public int compare(SmsMsg a, SmsMsg b) { return Long.compare(a.date, b.date); } });
      return out.size() > limit ? new ArrayList<SmsMsg>(out.subList(0, limit)) : out;
    }
    public byte[] partBytes(SmsPart p) throws Exception {
      return SmsSweep.readWhole(store, p.id);
    }
    public String putBlob(byte[] plain, String mime, String name) throws Exception {
      String sha = String.format("%064x", new java.math.BigInteger(1,
          java.security.MessageDigest.getInstance("SHA-256").digest(plain)));
      blobs.put(sha, plain);
      return sha;
    }
    public JSONObject seal(String doc, String bodyJson) throws Exception {
      if (sealThrows) throw new Exception("no key on this device");
      sealed++;
      docs.add(doc);
      bodies.add(bodyJson);
      return new JSONObject();          // opaque here: the phone signs it, this test does not read it
    }
    public long mark() { return mark; }
    public void mark(long ms) { mark = ms; }
  }

  static SmsMsg text(String addr, long date, String body, boolean in) {
    SmsMsg m = new SmsMsg();
    m.address = addr; m.date = date; m.body = body; m.type = in ? 1 : 2;
    return m;
  }
  static SmsMsg pic(World w, String addr, long date, long partId, int size, boolean refuse) {
    SmsMsg m = text(addr, date, "", true);
    m.mms = true;
    SmsPart p = new SmsPart();
    p.id = partId; p.ct = "image/jpeg"; p.name = "IMG_" + partId + ".jpg"; p.bytes = size;
    m.parts.add(p);
    byte[] b = new byte[size];
    for (int i = 0; i < size; i++) b[i] = (byte) ((i * 31 + partId) & 0xff);
    w.store.bytes.put(partId, b);
    if (refuse) w.store.refuse.add(partId);
    return m;
  }
  static Map<String, Object> bodyOf(World w, int i) {
    return Json.obj(Json.parse(w.bodies.get(i)));
  }
  static Map<String, Object> att(World w, int i, int j) {
    return Json.obj(Json.arr(bodyOf(w, i).get("att")).get(j));
  }

  public static void main(String[] a) throws Exception {
    Map<String, Object> out = new LinkedHashMap<String, Object>();

    /* ---- A: an ordinary history goes up, oldest first, and the mark lands on the newest. */
    {
      World w = new World();
      w.rows.add(text("+15551234567", 1000, "hello", true));
      w.rows.add(text("+15551234567", 2000, "hi back", false));
      w.rows.add(pic(w, "+15559998888", 3000, 77, 4096, false));
      SmsSweep.Report r = SmsSweep.run(w, 50);
      out.put("A_error", r.error);
      out.put("A_rows", r.rows);
      out.put("A_published", r.published);
      out.put("A_attachments", r.attachments);
      out.put("A_refused", r.refused);
      out.put("A_blobs", w.blobs.size());
      out.put("A_mark_before_commit", w.mark);
      SmsSweep.commit(w, r);
      out.put("A_mark", w.mark);
      // The address is the one the JavaScript computes for the same row.
      out.put("A_doc0", w.docs.get(0));
      out.put("A_doc0_expected",
          SmsKeys.docId("+15551234567", 1000, "hello", true, ""));
      out.put("A_pic_mms", Json.bool(bodyOf(w, 2).get("mms"), false));
      out.put("A_pic_sha_len", Json.str(att(w, 2, 0).get("sha"), "").length());
      out.put("A_pic_has_err", att(w, 2, 0).containsKey("err"));
      // A picture message's address counts its attachments in, or two photos in one second
      // overwrite each other.
      out.put("A_pic_doc_uses_parts", !w.docs.get(2).equals(
          SmsKeys.docId("+15559998888", 3000, "", true, "")));

      // …and a second sweep, with nothing new, does nothing at all.
      SmsSweep.Report r2 = SmsSweep.run(w, 50);
      out.put("A2_rows", r2.rows);
      out.put("A2_published", r2.published);
    }

    /* ---- B: A REFUSED ATTACHMENT MUST NOT COST ITS MESSAGE, AND MUST NOT FREEZE THE MARK.
     * The refusals are at the OLD end, which is where the provider starts. */
    {
      World w = new World();
      w.rows.add(pic(w, "+1555", 1000, 1, 2048, true));
      w.rows.add(pic(w, "+1555", 2000, 2, 2048, true));
      w.rows.add(text("+1555", 3000, "a text behind the refusals", true));
      w.rows.add(pic(w, "+1555", 4000, 4, 2048, false));
      SmsSweep.Report r = SmsSweep.run(w, 50);
      SmsSweep.commit(w, r);
      out.put("B_published", r.published);
      out.put("B_refused", r.refused);
      out.put("B_attachments", r.attachments);
      out.put("B_mark", w.mark);
      out.put("B_err_recorded", Json.str(att(w, 0, 0).get("err"), ""));
      out.put("B_still_mms", Json.bool(bodyOf(w, 0).get("mms"), false));
      out.put("B_sha_empty", Json.str(att(w, 0, 0).get("sha"), "").isEmpty());
      // The next pass starts AFTER the refusals: they are archived, named, and done.
      SmsSweep.Report r2 = SmsSweep.run(w, 50);
      out.put("B2_rows", r2.rows);
    }

    /* ---- C: A CHUNK CAP IS NOT A FILE SIZE. A photo larger than one read still gets archived. */
    {
      World w = new World();
      int big = 3 * 1024 * 1024;                 // well over the 512 KB single read
      w.rows.add(pic(w, "+1555", 1000, 9, big, false));
      SmsSweep.Report r = SmsSweep.run(w, 50);
      out.put("C_attachments", r.attachments);
      out.put("C_refused", r.refused);
      out.put("C_paged", w.store.wholeReads > 1);
      String sha = Json.str(att(w, 0, 0).get("sha"), "");
      out.put("C_bytes_stored", w.blobs.get(sha) == null ? -1 : w.blobs.get(sha).length);
      out.put("C_bytes_wanted", big);
    }

    /* ---- D: a part the provider will not size is refused, not truncated. */
    {
      World w = new World();
      w.rows.add(pic(w, "+1555", 1000, 5, 3 * 1024 * 1024, false));
      w.store.noSize.add(5L);
      SmsSweep.Report r = SmsSweep.run(w, 50);
      out.put("D_refused", r.refused);
      out.put("D_attachments", r.attachments);
      out.put("D_published", r.published);
      out.put("D_err", Json.str(att(w, 0, 0).get("err"), ""));
    }

    /* ---- E: THE MARK MOVES ONLY WHEN THE CALLER SAYS SO. A sweep whose events never reached a
     * relay must be repeatable, or that window of history is lost with nothing said. */
    {
      World w = new World();
      w.rows.add(text("+1555", 1000, "one", true));
      w.rows.add(text("+1555", 2000, "two", true));
      SmsSweep.Report r = SmsSweep.run(w, 50);
      out.put("E_published", r.published);
      out.put("E_mark_still_zero", w.mark == 0);
      SmsSweep.Report again = SmsSweep.run(w, 50);      // no commit: the same window comes back
      out.put("E_repeatable", again.published);
      SmsSweep.commit(w, again);
      out.put("E_after_commit", SmsSweep.run(w, 50).published);
    }

    /* ---- F: a pass is BOUNDED, and says there is more. An unbounded sweep on a phone somebody is
     * holding is the "encrypting and copying messages to blossom makes it glitchy" report. */
    {
      World w = new World();
      for (int i = 1; i <= 40; i++) w.rows.add(text("+1555", i * 1000, "m" + i, true));
      SmsSweep.Report r = SmsSweep.run(w, 10);
      SmsSweep.commit(w, r);
      out.put("F_published", r.published);
      out.put("F_more", r.more);
      out.put("F_mark", w.mark);
      SmsSweep.Report r2 = SmsSweep.run(w, 10);
      out.put("F2_published", r2.published);
      out.put("F2_first_is_new", Json.str(bodyOf(w, 10).get("body"), ""));
    }

    /* ---- G: a device with no key publishes nothing and moves nothing, rather than half a pass. */
    {
      World w = new World();
      w.rows.add(text("+1555", 1000, "one", true));
      w.sealThrows = true;
      SmsSweep.Report r = SmsSweep.run(w, 50);
      SmsSweep.commit(w, r);
      out.put("G_published", r.published);
      out.put("G_error_said", !r.error.isEmpty());
      out.put("G_mark", w.mark);
    }

    /* ---- H: an unaddressed row has no stable identity and is skipped without stopping the pass. */
    {
      World w = new World();
      w.rows.add(text("", 1000, "from nobody", true));
      w.rows.add(text("+1555", 2000, "from somebody", true));
      SmsSweep.Report r = SmsSweep.run(w, 50);
      SmsSweep.commit(w, r);
      out.put("H_published", r.published);
      out.put("H_rows", r.rows);
      out.put("H_mark", w.mark);
    }

    System.out.println(Json.write(out));
  }
}
"""


def _need(*tools):
    for t in tools:
        if not shutil.which(t):
            pytest.skip("%s is not installed here" % t)


_CACHE = {}


def result():
    if "r" in _CACHE:
        return _CACHE["r"]
    _need("javac", "java")
    with tempfile.TemporaryDirectory() as tmp:
        drv = os.path.join(tmp, "Drv.java")
        with open(drv, "w", encoding="utf-8") as fh:
            fh.write(DRIVER)
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        c = subprocess.run(["javac", "-nowarn", "-d", out, "-sourcepath",
                            STUBS + os.pathsep + os.path.join(ANDROID, "src", "main", "java")]
                           + SRC + [drv], capture_output=True, text=True, timeout=600)
        assert c.returncode == 0, c.stderr[-6000:]
        r = subprocess.run(["java", "-cp", out, "place.poster.app.sms.Drv"],
                           capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, (r.stdout[-2000:] + r.stderr[-6000:])
        _CACHE["r"] = json.loads(r.stdout.strip().splitlines()[-1])
    return _CACHE["r"]


def test_a_native_sweep_archives_the_phones_history_with_no_webview():
    """The whole point: the launcher's Texts app can back up messages on its own."""
    r = result()
    assert r["A_error"] == "", r
    assert r["A_rows"] == 3, r
    assert r["A_published"] == 3, r
    assert r["A_attachments"] == 1, r
    assert r["A_blobs"] == 1, "the picture never reached the encrypted drive: %r" % r


def test_the_address_is_the_one_the_javascript_computes():
    """Two spellings of one message is two documents, and the thread shows it twice. SmsKeys is
    already held to the JavaScript in tests/test_android_sms.py; this is the sweep USING it."""
    r = result()
    assert r["A_doc0"] == r["A_doc0_expected"], r
    assert r["A_doc0"].startswith("pcai:sms:"), r


def test_a_picture_messages_address_counts_its_attachments_in():
    """A picture message frequently has no text, so who/when/direction/body is the identical string
    for two photos sent inside one second — filed at one address, one of them is simply gone."""
    r = result()
    assert r["A_pic_doc_uses_parts"] is True, r
    assert r["A_pic_mms"] is True, r
    assert r["A_pic_sha_len"] == 64, "the picture was archived without a hash: %r" % r
    assert r["A_pic_has_err"] is False, r


def test_a_second_sweep_with_nothing_new_does_nothing():
    r = result()
    assert r["A2_rows"] == 0, r
    assert r["A2_published"] == 0, r


def test_a_refused_attachment_does_not_freeze_the_mark():
    """THE BUG THIS EXISTS FOR. The provider answers oldest-first, so two permanent refusals at the
    old end stood in front of everything newer: 213 rows read, `published: 0`, the mark unchanged
    sweep after sweep — indistinguishable from a relay that stopped accepting, and reported as
    exactly that."""
    r = result()
    assert r["B_published"] == 4, "a refusal cost its message: %r" % r
    assert r["B_refused"] == 2, r
    assert r["B_attachments"] == 1, r
    assert r["B_mark"] == 4000, "the mark froze at the refusal: %r" % r
    assert r["B2_rows"] == 0, "the refusals came back for ever: %r" % r


def test_a_refusal_is_published_with_its_reason_and_stays_a_picture_message():
    """An empty bubble is indistinguishable from a message that never had a picture. The reason is
    the only way any other device can say what happened, or count how many are affected."""
    r = result()
    assert r["B_err_recorded"], "nothing said why: %r" % r
    assert r["B_still_mms"] is True, r
    assert r["B_sha_empty"] is True, r


def test_a_chunk_cap_is_not_a_file_size():
    """Read as "no bytes", a null over the cap is how every camera photo quietly stopped being
    archived — 1,284 of one account's 1,964 documents flagged `mms:true` carrying no attachment."""
    r = result()
    assert r["C_attachments"] == 1, "a large photo was refused: %r" % r
    assert r["C_refused"] == 0, r
    assert r["C_paged"] is True, "it never paged — the fixture is not testing what it claims: %r" % r
    assert r["C_bytes_stored"] == r["C_bytes_wanted"], "the photo was truncated: %r" % r


def test_a_part_that_cannot_be_sized_is_refused_rather_than_truncated():
    """Half a photo stored under a content hash is worse than none: it is unreadable AND it looks
    archived."""
    r = result()
    assert r["D_refused"] == 1, r
    assert r["D_attachments"] == 0, r
    assert r["D_published"] == 1, "the message was lost with its attachment: %r" % r
    assert r["D_err"], r


def test_the_mark_moves_only_when_the_caller_has_published():
    """A sweep that advanced its own mark with no relay connected would throw the window away, and
    the next pass would start after messages nobody ever archived."""
    r = result()
    assert r["E_published"] == 2, r
    assert r["E_mark_still_zero"] is True, "it advanced its own mark: %r" % r
    assert r["E_repeatable"] == 2, "the uncommitted window did not come back: %r" % r
    assert r["E_after_commit"] == 0, r


def test_a_pass_is_bounded_and_says_there_is_more():
    """This runs on a phone somebody is holding. "Encrypting and copying messages to blossom makes
    it glitchy" is what an unbounded pass feels like."""
    r = result()
    assert r["F_published"] == 10, r
    assert r["F_more"] is True, r
    assert r["F_mark"] == 10000, r
    assert r["F2_published"] == 10, r
    assert r["F2_first_is_new"] == "m11", "the second pass re-read the first window: %r" % r


def test_a_device_with_no_key_publishes_nothing_and_moves_nothing():
    r = result()
    assert r["G_published"] == 0, r
    assert r["G_error_said"] is True, "it failed silently: %r" % r
    assert r["G_mark"] == 0, "it marked history as archived that it never sealed: %r" % r


def test_an_unaddressed_row_is_skipped_without_stopping_the_pass():
    r = result()
    assert r["H_rows"] == 2, r
    assert r["H_published"] == 1, r
    assert r["H_mark"] == 2000, "one bad row stranded everything behind it: %r" % r
