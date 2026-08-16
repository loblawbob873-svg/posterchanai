"""The native half of folder sync: the phone's own copy of the drive-blob format.

Background folder sync on Android cannot be done from inside the WebView — Chromium throttles a
hidden page's JavaScript however awake the processor is, which is a browser policy and not a power
one, so the alarm, the wake lock and its renewal were all necessary and none of them could touch it.
The transfer therefore moves into Java, where this app's other background work (push, the media
session, the signer) already lives and where every other sync app on Android does it.

Moving it means writing a SECOND implementation of a format that already exists, and that is the
danger this file exists for. A blob is `iv(12) || AES-256-GCM(bytes)` under the drive master key with
the IV derived from the CONTENT, and the address of a blob is the sha256 of that ciphertext. Nothing
about a wrong port fails loudly:

  * an IV that is random, or sliced differently, still encrypts and still decrypts on the device that
    wrote it — and every device re-uploads its whole folder for ever, because no two of them can
    agree on an address any more;
  * a base64 or JSON difference in the wrapped key produces "cannot decrypt", blamed on the network;
  * a JSON writer that escapes a forward slash (org.json does) changes the manifest's bytes — this
    codebase has a feature on record that silently stopped working for exactly that reason.

So the Java is not asserted against a description of the format. It is RUN, and its output is
compared with node running the same operations the way `app.js` does — and both directions of
interop are checked, because equal ciphertext and mutual decryptability are different claims and only
the second one is what a phone and a laptop actually need from each other.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
JAVA = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app")
STUBS = os.path.join(ROOT, "tests", "androidstubs")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


APPJS = _read(ROOT, "static", "js", "client", "app.js")

SYNC_SRC = [
    os.path.join(JAVA, "sync", "Json.java"),
    os.path.join(JAVA, "sync", "SyncCrypto.java"),
    os.path.join(JAVA, "signer", "Crypt.java"),
    os.path.join(JAVA, "signer", "Nostr.java"),
    os.path.join(JAVA, "signer", "Native.java"),
]


def _need(*tools):
    for t in tools:
        if shutil.which(t) is None:
            pytest.skip("no " + t)


def run_java(body, name="Driver"):
    """Compile the real sync sources plus a throwaway driver and run it, returning stdout.

    The driver joins the sync package so a helper does not have to be public to be tested."""
    _need("javac", "java")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, name + ".java")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("package place.poster.app.sync;\npublic class %s {\n"
                     "  public static void main(String[] argv) throws Exception {\n%s\n  }\n}\n"
                     % (name, body))
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        c = subprocess.run(["javac", "-nowarn", "-d", out, "-sourcepath",
                            STUBS + os.pathsep + os.path.join(ANDROID, "src", "main", "java")]
                           + SYNC_SRC + [src],
                           capture_output=True, text=True, timeout=300)
        assert c.returncode == 0, c.stderr[-4000:]
        r = subprocess.run(["java", "-cp", out, "place.poster.app.sync." + name],
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stderr[-4000:]
        return r.stdout.strip()


def run_node(script):
    _need("node")
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "drive.mjs")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(script)
        r = subprocess.run(["node", p], capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stderr[-4000:]
        return r.stdout.strip()


# The browser's side of the format, copied from app.js. The test below asserts these lines are still
# what app.js says — a copy that drifts is worse than no copy, because it would agree with itself.
JS_DRIVE = """
const hex = b => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2,'0')).join('');
const unhex = s => new Uint8Array(s.match(/../g).map(h => parseInt(h,16)));
async function sha256hex(buf){ const h=await crypto.subtle.digest('SHA-256', buf);
  return [...new Uint8Array(h)].map(b=>b.toString(16).padStart(2,'0')).join(''); }
async function _contentIV(plain){ return new Uint8Array(await crypto.subtle.digest('SHA-256', plain)).slice(0,12); }
async function _masterEncrypt(mk, plain, iv){ iv = iv || crypto.getRandomValues(new Uint8Array(12));
  const ck=await crypto.subtle.importKey('raw',mk,'AES-GCM',false,['encrypt']);
  const ct=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv},ck,plain));
  const out=new Uint8Array(12+ct.length); out.set(iv,0); out.set(ct,12); return out; }
async function _masterDecrypt(mk, blob){ const iv=blob.slice(0,12), ct=blob.slice(12);
  const ck=await crypto.subtle.importKey('raw',mk,'AES-GCM',false,['decrypt']);
  return new Uint8Array(await crypto.subtle.decrypt({name:'AES-GCM',iv},ck,ct)); }
"""

# A key and three payloads: empty-ish, a short string, and something past one AES block so a wrong
# tag position or a wrong counter start shows up.
MK_HEX = "00112233445566778899aabbccddeeff102132435465768798a9bacbdcedfe0f"
PAYLOADS = ["a", "hello world", "x" * 200, "a/b/c — ünïcode ☃ name.txt"]


def test_the_java_and_the_browser_produce_the_same_blob_and_the_same_address():
    """Byte-for-byte, because the address IS the sha of these bytes: two devices that disagree here
    cannot dedup, and each one re-uploads the whole folder on every sweep it ever runs."""
    js = run_node(JS_DRIVE + """
const mk = unhex('%s');
for(const p of %s){
  const plain = new TextEncoder().encode(p);
  const blob = await _masterEncrypt(mk, plain, await _contentIV(plain));
  console.log(hex(blob) + ' ' + await sha256hex(blob));
}
""" % (MK_HEX, json.dumps(PAYLOADS)))

    java = run_java("""
    byte[] mk = place.poster.app.signer.Nostr.unhex("%s");
    String[] ps = new String[]{%s};
    for(String p : ps){
      byte[] blob = SyncCrypto.encrypt(mk, SyncCrypto.utf8(p));
      System.out.println(place.poster.app.signer.Nostr.hex(blob) + " " + SyncCrypto.sha256hex(blob));
    }
""" % (MK_HEX, ", ".join(json.dumps(p) for p in PAYLOADS)))

    assert js.splitlines() == java.splitlines()
    assert len(js.splitlines()) == len(PAYLOADS)


def test_each_side_can_open_what_the_other_sealed():
    """Equal ciphertext is not the same claim as mutual decryptability, and it is the second one a
    phone and a laptop need from each other. A tag length or an IV-length assumption that differs
    would pass the vector test above by accident and fail here."""
    java_blob = run_java("""
    byte[] mk = place.poster.app.signer.Nostr.unhex("%s");
    System.out.println(place.poster.app.signer.Nostr.hex(SyncCrypto.encrypt(mk, SyncCrypto.utf8("from the phone"))));
""" % MK_HEX)
    back = run_node(JS_DRIVE + """
const mk = unhex('%s');
const plain = await _masterDecrypt(mk, unhex('%s'));
console.log(new TextDecoder().decode(plain));
const mine = await _masterEncrypt(mk, new TextEncoder().encode('from the laptop'), await _contentIV(new TextEncoder().encode('from the laptop')));
console.log(hex(mine));
""" % (MK_HEX, java_blob))
    said, js_blob = back.splitlines()
    assert said == "from the phone"
    opened = run_java("""
    byte[] mk = place.poster.app.signer.Nostr.unhex("%s");
    System.out.println(SyncCrypto.fromUtf8(SyncCrypto.decrypt(mk, place.poster.app.signer.Nostr.unhex("%s"))));
""" % (MK_HEX, js_blob))
    assert opened == "from the laptop"


def test_the_iv_is_the_content_hash_and_not_a_random_one():
    """The dedup, stated as a property rather than a vector: encrypting the same bytes twice must
    give the same blob, and the IV must be the first 12 bytes of their sha256. A random IV passes
    every round-trip test in this file and quietly turns every sweep into a full re-upload."""
    out = run_java("""
    byte[] mk = new byte[32];
    byte[] plain = SyncCrypto.utf8("the same bytes");
    byte[] a = SyncCrypto.encrypt(mk, plain), b = SyncCrypto.encrypt(mk, plain);
    System.out.println(java.util.Arrays.equals(a, b));
    System.out.println(place.poster.app.signer.Nostr.hex(java.util.Arrays.copyOf(a, 12)));
    System.out.println(SyncCrypto.sha256hex(plain).substring(0, 24));
""")
    same, iv, head = out.splitlines()
    assert same == "true", "the same bytes encrypted to two different blobs — dedup is gone"
    assert iv == head


def test_the_browser_side_of_the_format_still_says_what_this_port_mirrors():
    """The vectors above are checked against a COPY of app.js's crypto, which is only meaningful
    while the copy is accurate. If someone changes the drive format in the browser, this fails here
    instead of on a phone that has silently stopped agreeing with every other device."""
    for line in ("async function _contentIV(plain){ return new Uint8Array(await crypto.subtle.digest"
                 "('SHA-256', plain)).slice(0,12); }",
                 "const out=new Uint8Array(12+ct.length); out.set(iv,0); out.set(ct,12); return out; }",
                 "async function _masterDecrypt(mk, blob){ const iv=blob.slice(0,12), ct=blob.slice(12);"):
        assert line in APPJS, "app.js changed the drive blob format: " + line[:60]


def test_the_wrapped_drive_key_unwraps_with_the_nostr_key_the_signer_already_holds():
    """No new secret at rest is the whole reason the native path is acceptable: the phone stores the
    WRAPPED key and opens it with the account key the signer already has. A wrong-sized key throws
    rather than returning something that decrypts nothing and gets blamed on the media server."""
    out = run_java("""
    byte[] sec = place.poster.app.signer.Nostr.unhex(
        "1111111111111111111111111111111111111111111111111111111111111111");
    byte[] mk = new byte[32];
    for (int i = 0; i < 32; i++) mk[i] = (byte) (i * 7);
    String wrapped = SyncCrypto.sealToSelf(sec, "{\\"k\\":\\"" + place.poster.app.signer.Crypt.b64(mk) + "\\"}");
    System.out.println(place.poster.app.signer.Nostr.hex(SyncCrypto.unwrapMasterKey(sec, wrapped)));
    System.out.println(place.poster.app.signer.Nostr.hex(mk));
    try {
      SyncCrypto.unwrapMasterKey(sec, SyncCrypto.sealToSelf(sec, "{\\"k\\":\\"c2hvcnQ=\\"}"));
      System.out.println("accepted-a-short-key");
    } catch (IllegalArgumentException e) { System.out.println("refused"); }
""")
    got, want, short = out.splitlines()
    assert got == want
    assert short == "refused"


# ------------------------------------------------------------------------------- the JSON codec

JSON_CASES = [
    {"a": 1, "b": "two"},
    {"path/with/slashes.txt": {"sha": "ab", "size": 1234567890123, "mtime": 1700000000000}},
    {"quote\"and\\back": "tab\tnew\nline"},
    {"unicode": "ünïcode ☃ 日本語"},
    {"deep": {"list": [1, 2, {"x": True}, None, "s"], "empty": {}, "none": []}},
    {"zero": 0, "neg": -17, "big": 9007199254740991},
]


def test_the_json_written_here_is_the_json_the_browser_would_have_written():
    """The manifest is serialised, encrypted and hashed, and read by browsers. org.json escapes a
    forward slash — legal JSON, different bytes — and nearly every path in a manifest contains one."""
    java = run_java("""
    String[] src = new String[]{%s};
    for (String s : src) System.out.println(Json.write(Json.parse(s)));
""" % ", ".join(json.dumps(json.dumps(c, separators=(",", ":"))) for c in JSON_CASES))
    js = run_node("""
const src = %s;
for (const s of src) console.log(JSON.stringify(JSON.parse(s)));
""" % json.dumps([json.dumps(c, separators=(",", ":")) for c in JSON_CASES]))
    assert java.splitlines() == js.splitlines()
    assert all("\\/" not in line for line in java.splitlines())


def test_an_integer_does_not_come_back_as_a_decimal():
    """A size or an mtime written as `1234.0` is a different string, a different sha, and — for a
    manifest another device compares against — a file that looks changed on every sweep."""
    assert run_java("""
    System.out.println(Json.write(Json.parse("{\\"size\\":1234,\\"mtime\\":1700000000000}")));
""") == '{"size":1234,"mtime":1700000000000}'


# ------------------------------------------------------------------- the Android-touching half

ANDROID_SRC = [os.path.join(JAVA, "sync", f + ".java") for f in
               ("SafFs", "NativeSweep", "SyncStore", "SyncNet", "SyncCrypto", "SyncDiff",
                "Json", "Excludes")]


def test_the_android_half_type_checks_here_rather_than_on_ci():
    """SafFs, NativeSweep and SyncStore touch the platform, so they cannot be RUN here — but they can
    be compiled against the hand-written stubs, which turns a wrong column constant or a dropped
    argument into a failing test in a second instead of a broken APK build twenty minutes later.

    It matters more for these than for anything else in the app: there is no device in this loop at
    all, so a compile error is discovered by a person with a phone."""
    _need("javac")
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(["javac", "-nowarn", "-d", tmp, "-sourcepath",
                            STUBS + os.pathsep + os.path.join(ANDROID, "src", "main", "java")]
                           + ANDROID_SRC, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-4000:]
