"""What the phone puts on the wire when it syncs a folder without the WebView.

The native sweep talks to two servers — the Blossom media server for the encrypted blobs, and this
node's /client/sync-manifest for the shared agreement — and every one of those requests used to be
made by JavaScript that this repo tests elsewhere. Rewriting them in Java means the SHAPES can drift,
and each way they can drift is silent:

  * an auth event this node's own verifier rejects. `_verify_self_auth` wants a signed event by that
    pubkey inside a five-minute window; a phone that signs it wrong gets a 403 that reads exactly like
    "you are not signed in", which is the least likely thing to be investigated.
  * a Blossom upload without `X-Keep`. It works, the sweep reports success, and the media server's age
    sweep deletes everybody's synced files some weeks later.
  * skipping an upload because the server HEADs 200 — while the blob is carrying an expiry and is on
    its way out. The manifest entry then points at bytes due to vanish.
  * a 409 collapse read as an ordinary failure. That is the server refusing to shrink the folder, and
    treating it as "try again" is what made a legitimate mass delete impossible for ever.
  * the server storing a different sha than the one we hashed, ignored — every other device then
    fails to download a file this one reports as synced.

So the Java is RUN against a real HTTP server in the test, and the request it makes is verified with
THIS REPO'S OWN verifier — the same function the endpoint calls — rather than by describing it.
"""
import base64
import json
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
JAVA = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app")
STUBS = os.path.join(ROOT, "tests", "androidstubs")

SRC = [
    os.path.join(JAVA, "sync", "Json.java"),
    os.path.join(JAVA, "sync", "SyncCrypto.java"),
    os.path.join(JAVA, "sync", "SyncNet.java"),
    os.path.join(JAVA, "signer", "Crypt.java"),
    os.path.join(JAVA, "signer", "Nostr.java"),
    os.path.join(JAVA, "signer", "Native.java"),
]

SEC = "1111111111111111111111111111111111111111111111111111111111111111"

# A stub of the two servers, plus the calls a sweep makes against them. Everything the server SAW is
# printed as JSON so the assertions below can be about the request rather than about the client's
# opinion of it.
DRIVER = r"""
    final java.util.List<String> seen = new java.util.ArrayList<String>();
    com.sun.net.httpserver.HttpServer srv =
        com.sun.net.httpserver.HttpServer.create(new java.net.InetSocketAddress("127.0.0.1", 0), 0);
    final byte[] stored = "ciphertext-bytes".getBytes("UTF-8");
    final String storedSha = SyncCrypto.sha256hex(stored);

    com.sun.net.httpserver.HttpHandler h = new com.sun.net.httpserver.HttpHandler() {
      public void handle(com.sun.net.httpserver.HttpExchange x) throws java.io.IOException {
        String path = x.getRequestURI().getPath();
        java.io.ByteArrayOutputStream bo = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[8192]; int n;
        while ((n = x.getRequestBody().read(buf)) > 0) bo.write(buf, 0, n);
        byte[] body = bo.toByteArray();

        java.util.Map<String,Object> rec = new java.util.LinkedHashMap<String,Object>();
        rec.put("method", x.getRequestMethod());
        rec.put("path", path);
        rec.put("auth", x.getRequestHeaders().getFirst("Authorization"));
        rec.put("keep", x.getRequestHeaders().getFirst("X-Keep"));
        rec.put("nomirror", x.getRequestHeaders().getFirst("X-No-Mirror"));
        rec.put("ctype", x.getRequestHeaders().getFirst("Content-Type"));
        rec.put("body", new String(body, "UTF-8"));
        seen.add(Json.write(rec));

        byte[] out; int code = 200;
        if (path.endsWith("/upload")) {
          out = ("{\"url\":\"https://media.example/" + SyncCrypto.sha256hex(body) + ".bin\"}")
                 .getBytes("UTF-8");
        } else if (path.endsWith("/expiring")) {
          x.getResponseHeaders().add("X-Expires-At", "1900000000");
          out = new byte[0];
        } else if (path.endsWith("/missing")) {
          code = 404; out = new byte[0];
        } else if (path.startsWith("/collapse")) {
          code = 409;
          out = "{\"ok\":false,\"error\":\"refused\",\"collapse\":{\"old\":900,\"new\":3}}".getBytes("UTF-8");
        } else if (path.endsWith("/sync-manifest")) {
          out = "{\"ok\":true,\"manifest\":{\"n\":2,\"sealed\":\"xx\"}}".getBytes("UTF-8");
        } else {
          out = stored;
        }
        if ("HEAD".equals(x.getRequestMethod())) { x.sendResponseHeaders(code, -1); x.close(); return; }
        x.sendResponseHeaders(code, out.length);
        x.getResponseBody().write(out);
        x.close();
      }
    };
    srv.createContext("/", h);
    srv.start();
    String base = "http://127.0.0.1:" + srv.getAddress().getPort();

    SyncNet net = new SyncNet(base, base + "/blossom", place.poster.app.signer.Nostr.unhex("SECRET"));
    java.util.Map<String,Object> results = new java.util.LinkedHashMap<String,Object>();

    results.put("exists_plain", net.blobExists(storedSha));
    results.put("exists_expiring", net.blobExists("expiring"));
    results.put("exists_missing", net.blobExists("missing"));
    results.put("got", new String(net.getBlob(storedSha), "UTF-8"));
    results.put("put", net.putBlob("hello blossom".getBytes("UTF-8")));
    results.put("put_expected", SyncCrypto.sha256hex("hello blossom".getBytes("UTF-8")));

    java.util.Map<String,Object> read = net.manifest("Documents", null, false);
    results.put("read_ok", Json.bool(read.get("ok"), false));
    java.util.Map<String,Object> doc = new java.util.LinkedHashMap<String,Object>();
    doc.put("n", 7L); doc.put("sealed", "sealed-blob");
    net.manifest("Documents", doc, false);

    try {
      SyncNet bad = new SyncNet(base + "/collapse", base + "/blossom",
                                place.poster.app.signer.Nostr.unhex("SECRET"));
      bad.manifest("Documents", doc, false);
      results.put("collapse", "not-raised");
    } catch (SyncNet.Collapse c) {
      results.put("collapse", "old=" + c.oldCount + " new=" + c.newCount + " shrink=" + c.shrink());
    }

    srv.stop(0);
    java.util.Map<String,Object> all = new java.util.LinkedHashMap<String,Object>();
    all.put("results", results);
    java.util.List<Object> reqs = new java.util.ArrayList<Object>();
    for (String s : seen) reqs.add(Json.parse(s));
    all.put("requests", reqs);
    System.out.println(Json.write(all));
""".replace("SECRET", SEC)


def _run():
    if shutil.which("javac") is None or shutil.which("java") is None:
        pytest.skip("no JDK")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "NetDriver.java")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("package place.poster.app.sync;\npublic class NetDriver {\n"
                     "  public static void main(String[] a) throws Exception {\n%s\n  }\n}\n" % DRIVER)
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        c = subprocess.run(["javac", "-nowarn", "-d", out, "-sourcepath",
                            STUBS + os.pathsep + os.path.join(ANDROID, "src", "main", "java")]
                           + SRC + [src], capture_output=True, text=True, timeout=300)
        assert c.returncode == 0, c.stderr[-4000:]
        r = subprocess.run(["java", "-cp", out, "place.poster.app.sync.NetDriver"],
                           capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, r.stderr[-4000:]
        return json.loads(r.stdout.strip())


@pytest.fixture(scope="module")
def wire():
    return _run()


def test_the_transfer_does_what_it_says(wire):
    r = wire["results"]
    assert r["got"] == "ciphertext-bytes"
    assert r["put"] == r["put_expected"], "the sha recorded is not the sha of what was sent"
    assert r["read_ok"] is True


def test_present_is_not_enough_a_blob_on_its_way_out_is_uploaded_again(wire):
    r = wire["results"]
    assert r["exists_plain"] is True
    assert r["exists_expiring"] is False, "a blob carrying an expiry was treated as safely stored"
    assert r["exists_missing"] is False


def test_a_refused_shrink_arrives_as_something_the_caller_can_answer(wire):
    assert wire["results"]["collapse"] == "old=900 new=3 shrink=897"


def test_the_upload_is_marked_keep_and_no_mirror(wire):
    put = [q for q in wire["requests"] if q["path"].endswith("/upload")]
    assert len(put) == 1
    assert put[0]["method"] == "PUT"
    assert put[0]["keep"] == "1", "without X-Keep the media server's age sweep reclaims synced files"
    assert put[0]["nomirror"] == "1"
    assert put[0]["ctype"] == "application/octet-stream"


def test_this_nodes_own_verifier_accepts_the_phones_auth():
    """The endpoint calls `verify_self_auth`; so does this. A signature the phone is happy with and
    the server is not produces a 403 that reads like being signed out."""
    from app.services.nostr import event as nostr_event

    w = _run()
    posts = [q for q in w["requests"] if q["path"] == "/client/sync-manifest"]
    assert posts, "the sweep never posted a manifest"
    for q in posts:
        body = json.loads(q["body"])
        assert nostr_event.verify_self_auth(body["auth"], body["pubkey"]), \
            "this node would answer 403 to the phone's own proof of ownership"

    uploads = [q for q in w["requests"] if q["path"].endswith("/upload")]
    ev = json.loads(base64.b64decode(uploads[0]["auth"].split(" ", 1)[1]))
    assert ev["kind"] == 24242
    assert nostr_event.verify_event(ev), "the Blossom auth event does not verify"
    tags = {t[0]: t[1] for t in ev["tags"]}
    assert tags["t"] == "upload"
    assert tags["x"] == w["results"]["put_expected"], "the auth commits to a different blob than was sent"
    assert int(tags["expiration"]) > 0


def test_a_write_carries_the_manifest_and_a_read_does_not(wire):
    posts = [json.loads(q["body"]) for q in wire["requests"] if q["path"] == "/client/sync-manifest"]
    reads = [b for b in posts if "manifest" not in b]
    writes = [b for b in posts if "manifest" in b]
    assert len(reads) == 1 and len(writes) == 1
    assert writes[0]["manifest"] == {"n": 7, "sealed": "sealed-blob"}
    assert "force" not in writes[0], "an ordinary write must not carry force"
    assert reads[0]["folder"] == "Documents"
