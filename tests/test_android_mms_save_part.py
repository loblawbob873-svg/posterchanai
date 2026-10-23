"""THE NATIVE MESSAGES SCREEN'S SAVE, RUN ON A JVM: the shipped MmsPartSave + MediaSaveWriter + MediaSaveRules.

2026-10-23: "sms apk does not let me save media attachments". On the APK the Texts tile and every SMS
notification open the NATIVE ThreadActivity, which drew a picture you could only look at, a video you
could only play, no sound or file at all, and whose long-press menu was react / copy / delete. Each
attachment now has a Save that streams `content://mms/part/<id>` into Download/PosterChan/<kind>/ through
the SAME writer the WebView's MediaSave plugin uses.

What runs here is the shipped Java, compiled against the real android.jar (tests/androidcompile.py). What
is NOT shipped code, and why: `ContentResolver` / `ContentValues` / `Uri` / `MediaStore` / `Build` are
platform services with no JVM implementation, so at RUN time (only) they are a recorder of a MediaStore:
the rows inserted, the bytes written, the IS_PENDING flip, a delete -- and failures injected at each step.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import androidcompile as ac  # noqa: E402

JAVA = os.path.join(ROOT, "mobile/android/app/src/main/java/place/poster/app")
SOURCES = [os.path.join(JAVA, "preview", f) for f in ("MediaSaveWriter.java", "MediaSaveRules.java")] + \
          [os.path.join(JAVA, "sms", f) for f in ("MmsPartSave.java", "SmsPart.java", "SmsKeys.java")]

pytestmark = pytest.mark.skipif(not (shutil.which("javac") and shutil.which("java") and ac.android_jar()),
                                reason="JDK + android.jar required")

RUNTIME = {
    "android/net/Uri.java": r"""
package android.net;
public class Uri { public final String s; public Uri(String s){this.s=s;} @Override public String toString(){return s;} }
""",
    "android/os/Build.java": r"""
package android.os;
public class Build { public static class VERSION { public static int SDK_INT = 34; } }
""",
    "android/provider/MediaStore.java": r"""
package android.provider;
public final class MediaStore {
  public static final class Downloads { public static final android.net.Uri EXTERNAL_CONTENT_URI = new android.net.Uri("content://media/external/downloads"); }
}
""",
    "android/content/ContentValues.java": r"""
package android.content;
public final class ContentValues {
  public final java.util.LinkedHashMap<String,Object> m = new java.util.LinkedHashMap<>();
  public void put(String k, String v){m.put(k,v);} public void put(String k, Integer v){m.put(k,v);}
}
""",
    "android/content/ContentResolver.java": r"""
package android.content;
import java.io.*; import java.util.*;
/** RUNTIME ONLY: a recorder of MediaStore. `fail` names the step that throws. */
public class ContentResolver {
  public static class Row { public Map<String,Object> v = new LinkedHashMap<>(); public ByteArrayOutputStream bytes = new ByteArrayOutputStream(); public boolean deleted; }
  public final Map<String,Row> rows = new LinkedHashMap<>(); public final List<String> calls = new ArrayList<>();
  public String fail = ""; public String collection = "";
  public android.net.Uri insert(android.net.Uri u, ContentValues v){ calls.add("insert"); collection=u.toString();
    if(fail.equals("insert")) return null; String id=u+"/"+(rows.size()+1); Row r=new Row(); r.v.putAll(v.m); rows.put(id,r); return new android.net.Uri(id); }
  public OutputStream openOutputStream(android.net.Uri u) throws FileNotFoundException { calls.add("open");
    if(fail.equals("open")) throw new FileNotFoundException("No space left on device");
    final Row r=rows.get(u.toString()); return new OutputStream(){ int n=0;
      public void write(int b) throws IOException { write(new byte[]{(byte)b},0,1); }
      public void write(byte[] b,int o,int l) throws IOException { if(fail.equals("write") && (n+=l)>4096) throw new IOException("No space left on device"); r.bytes.write(b,o,l); } }; }
  public int update(android.net.Uri u, ContentValues v, String w, String[] a){ calls.add("update"); rows.get(u.toString()).v.putAll(v.m); return 1; }
  public int delete(android.net.Uri u, String w, String[] a){ calls.add("delete"); rows.get(u.toString()).deleted=true; return 1; }
}
""",
    "place/poster/app/sms/SaveHarness.java": r"""
package place.poster.app.sms;
import android.content.ContentResolver; import java.io.*; import java.util.*;
public class SaveHarness {
  static String q(String s){ StringBuilder b=new StringBuilder("\""); for(char c: s.toCharArray()){ if(c=='"'||c=='\\') b.append('\\'); b.append(c);} return b.append('"').toString(); }
  public static void main(String[] a) throws Exception {
    BufferedReader r=new BufferedReader(new InputStreamReader(System.in)); String line; StringBuilder out=new StringBuilder("[");
    while((line=r.readLine())!=null){ if(line.isEmpty()) continue;
      String[] f=line.split("\t",-1);   // ct, name, b64 bytes ("-" = the phone would not open it), fail, sdk
      SmsPart p=new SmsPart(); p.id=41; p.ct=f[0]; p.name=f[1];
      android.os.Build.VERSION.SDK_INT=Integer.parseInt(f[4]);
      ContentResolver cr=new ContentResolver(); cr.fail=f[3];
      InputStream in = f[2].equals("-") ? null : new ByteArrayInputStream(Base64.getDecoder().decode(f[2]));
      final boolean[] closed={false};
      if(in!=null){ final InputStream inner=in; in=new FilterInputStream(inner){ public void close() throws IOException { closed[0]=true; super.close(); } }; }
      String said=MmsPartSave.save(cr, in, p);
      StringBuilder rows=new StringBuilder("[");
      for(Map.Entry<String,ContentResolver.Row> e: cr.rows.entrySet()){ ContentResolver.Row row=e.getValue();
        if(rows.length()>1) rows.append(',');
        rows.append("{\"name\":").append(q(String.valueOf(row.v.get("_display_name")))).append(",\"mime\":").append(q(String.valueOf(row.v.get("mime_type"))))
            .append(",\"path\":").append(q(String.valueOf(row.v.get("relative_path")))).append(",\"pending\":").append(row.v.get("is_pending"))
            .append(",\"deleted\":").append(row.deleted).append(",\"b64\":").append(q(Base64.getEncoder().encodeToString(row.bytes.toByteArray()))).append('}'); }
      rows.append(']');
      if(out.length()>1) out.append(',');
      out.append("{\"said\":").append(q(said)).append(",\"savable\":").append(MmsPartSave.savable(p)).append(",\"collection\":").append(q(cr.collection))
         .append(",\"calls\":").append(q(String.join(",",cr.calls))).append(",\"closed\":").append(closed[0]).append(",\"rows\":").append(rows).append('}');
    }
    System.out.println(out.append(']'));
  }
}
""",
}


@pytest.fixture(scope="module")
def run():
    tmp = tempfile.mkdtemp(prefix="pc-mms-save-")
    r = ac.compile_sources(SOURCES, tmp)
    assert r.returncode == 0, "the save path does not compile against android.jar:\n" + r.stdout[-2000:] + r.stderr[-6000:]
    src = os.path.join(tmp, "rt-src")
    for rel, body in RUNTIME.items():
        p = os.path.join(src, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(body)
    rt = os.path.join(tmp, "rt")
    classes = os.path.join(tmp, "classes")
    r = subprocess.run(["javac", "-nowarn", "-d", rt, "-cp", os.pathsep.join([classes, ac.android_jar()])]
                       + [os.path.join(src, k) for k in RUNTIME], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    cp = os.pathsep.join([rt, classes, ac.android_jar()])

    def go(*cases):
        lines = "\n".join("\t".join(str(x) for x in c) for c in cases) + "\n"
        r = subprocess.run(["java", "-cp", cp, "place.poster.app.sms.SaveHarness"], input=lines,
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        return json.loads(r.stdout.strip().splitlines()[-1])
    yield go
    shutil.rmtree(tmp, ignore_errors=True)


def b64(b):
    import base64
    return base64.b64encode(b).decode()


PHOTO = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 40
VIDEO = b"\x00\x00\x00\x18ftyp3gp4" + bytes((i * 13) & 0xFF for i in range(150_000))   # spans many 64 KB reads
VOICE = b"#!AMR\n" + bytes((i * 7) & 0xFF for i in range(3000))


def test_every_kind_of_mms_part_is_written_whole_into_the_posterchan_tree(run):
    import base64
    out = run(("image/jpeg", "IMG_0412.jpg", b64(PHOTO), "", 34),
              ("video/3gpp", "", b64(VIDEO), "", 34),
              ("audio/amr", "voice", b64(VOICE), "", 34),
              ("text/x-vcard", "Dana.vcf", b64(b"BEGIN:VCARD\r\nEND:VCARD\r\n"), "", 34))
    want = [("Photo saved to Download/PosterChan/Images/IMG_0412.jpg", "IMG_0412.jpg", "image/jpeg", "Download/PosterChan/Images/", PHOTO),
            ("Video saved to Download/PosterChan/Videos/mms-video-41.3gp", "mms-video-41.3gp", "video/3gpp", "Download/PosterChan/Videos/", VIDEO),
            ("Audio saved to Download/PosterChan/Music/voice.amr", "voice.amr", "audio/amr", "Download/PosterChan/Music/", VOICE),
            ("Contact card saved to Download/PosterChan/Files/Dana.vcf", "Dana.vcf", "text/x-vcard", "Download/PosterChan/Files/", None)]
    for o, (said, name, mime, path, data) in zip(out, want):
        assert o["said"] == said, o
        assert o["collection"] == "content://media/external/downloads", o
        assert o["calls"] == "insert,open,update", o            # written, then published (IS_PENDING 0)
        assert o["closed"], "the part's stream was left open"
        [row] = o["rows"]
        assert (row["name"], row["mime"], row["path"], row["pending"], row["deleted"]) == (name, mime, path, 0, False), row
        if data is not None:
            assert base64.b64decode(row["b64"]) == data, (name, "the saved bytes are not the attachment")


def test_a_failed_save_says_why_and_leaves_no_broken_file_behind(run):
    out = run(("image/jpeg", "a.jpg", b64(PHOTO), "insert", 34),
              ("video/3gpp", "v.3gp", b64(VIDEO), "open", 34),
              ("video/3gpp", "v.3gp", b64(VIDEO), "write", 34),
              ("audio/amr", "x.amr", "-", "", 34),
              ("audio/amr", "x.amr", b64(b""), "", 34),
              ("image/png", "a.png", b64(PHOTO), "", 28))
    assert out[0]["said"] == "Could not save Photo: the gallery refused the file", out[0]
    # A row that was inserted and then could not be written is taken back out -- never a broken file.
    assert out[1]["said"] == "Could not save Video: No space left on device" and out[1]["rows"][0]["deleted"], out[1]
    assert out[2]["said"] == "Could not save Video: No space left on device" and out[2]["rows"][0]["deleted"], out[2]
    assert "update" not in out[2]["calls"], "a half-written file was published to the gallery"
    assert out[3]["said"] == "Could not save Audio: the phone would not open it" and out[3]["rows"] == [], out[3]
    assert out[4]["said"] == "Could not save Audio: file is empty" and out[4]["rows"][0]["deleted"], out[4]
    # Android 9: nothing is written (no permission-free public folder); ThreadActivity shares instead.
    assert out[5]["said"] == "Could not save Photo: saving to the phone needs Android 10 or newer" and out[5]["rows"] == [], out[5]


def test_the_words_and_the_layout_are_not_offered_as_attachments(run):
    out = run(("text/plain", "text_0.txt", b64(b"hi"), "", 34), ("application/smil", "smil.xml", b64(b"<smil/>"), "", 34),
              ("audio/amr", "v.amr", b64(VOICE), "", 34))
    assert [o["savable"] for o in out] == [False, False, True]


def test_the_messages_screen_puts_a_save_under_every_attachment():
    """ThreadActivity's wiring: every savable part gets a Save (including audio/other parts, which were
    not drawn at all), and the press runs MmsPartSave off the UI thread and SAYS the answer."""
    thread = open(os.path.join(JAVA, "sms", "ThreadActivity.java")).read()
    draw = thread[thread.index("private void drawParts("):thread.index("private final class Msgs")]
    assert "if (!MmsPartSave.savable(p)) continue;" in draw
    assert draw.count("host.addView(saveButton(p));") == 3, "image, video and every other part each get a Save"
    save = thread[thread.index("private void savePart("):thread.index("private void sharePart(")]
    assert "new Thread(" in save and "MmsPartSave.save(getContentResolver(), in, p)" in save and "say(said)" in save
    assert "if (!MediaSaveWriter.available()) { sharePart(p); return; }" in save
