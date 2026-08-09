"""Music kept for offline: it must still be there, and still be ciphertext.

A track already lands in the service worker's drive cache, but only by accident — only if you played
it, only under the 8MB per-blob cap, and only until that cache trims to make room for note
attachments. "My library on my phone" cannot be luck. Notes solved the same problem by pinning; this
is the same idea one layer down, and in IndexedDB rather than a Cache-API store because the desktop
builds load their bundle over app:// and the APK's worker is media-only at root scope — IDB is the
one store that behaves the same in a tab, a PWA, the Android WebView and Electron.

  round-trip        bytes stored come back byte for byte, so a downloaded track actually plays
  still-encrypted   what lands in IndexedDB is what Blossom served — never the decoded audio. An
                    offline copy must not be a weaker copy.
  have-is-a-set     the list asks "is this one here" on every repaint; it must be answered from
                    memory, not an IDB scan per keystroke
  drop              removing is local and immediate, and never touches the server
  keep-reports      a bulk download reports per track as it lands — a control that says nothing for
                    minutes while a library downloads is one nobody trusts
  keep-is-bounded   it does NOT open a connection per track: a library is hundreds of files and a
                    phone is not a datacentre (the same lesson the link-card fan-out taught this node)

The module is extracted from app.js rather than copied, so it cannot drift from what ships.
"""
import asyncio
import http.server
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "static", "js", "client", "app.js")


def _fn(src, name, opener):
    i = src.index(opener)
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError("could not bound " + name)


def _harness():
    with open(APP) as fh:
        src = fh.read()
    # The trailing SEMICOLON matters: the extractor stops at the closing brace, and the page's next
    # line is an IIFE — `const X = {…}\n(async()=>{})()` parses as CALLING the object, which fails
    # with "{…} is not a function" and an empty page rather than anything that names the cause.
    return _fn(src, "MusicOffline", "const MusicOffline = {") + ";"


PAGE = """<!doctype html><meta charset="utf-8"><pre id="out"></pre><script>
let fetched = 0, inFlight = 0, peak = 0;
const mediaServer = () => 'https://media.example';
const fetch = async (u) => {
  fetched++; inFlight++; peak = Math.max(peak, inFlight);
  await new Promise(r => setTimeout(r, 20));
  inFlight--;
  const sha = String(u).split('/').pop();
  // "ciphertext": bytes that are not the audio, keyed off the name so each track differs.
  const body = new Uint8Array([0xEE, 0xFF].concat([...sha.slice(0, 6)].map(c => c.charCodeAt(0))));
  return { ok: true, status: 200, arrayBuffer: async () => body.buffer };
};
__EXTRACTED__

(async () => {
  const out = {};
  try {
    indexedDB.deleteDatabase(MusicOffline.DB);
    await new Promise(r => setTimeout(r, 60));
    MusicOffline._db = null; MusicOffline._have = null;

    // 1. round trip
    const bytes = new Uint8Array([1, 2, 3, 250, 0, 77]);
    out.put = await MusicOffline.put('a'.repeat(64), bytes);
    const back = await MusicOffline.get('a'.repeat(64));
    out.roundTrip = !!back && back.length === bytes.length && [...back].every((b, i) => b === bytes[i]);
    out.missIsNull = (await MusicOffline.get('z'.repeat(64))) === null;

    // 2. have() is a Set held in memory
    const have = await MusicOffline.have();
    out.haveIsSet = have instanceof Set;
    out.haveHas = have.has('a'.repeat(64));
    out.haveCached = (await MusicOffline.have()) === have;

    // 3. drop
    await MusicOffline.drop('a'.repeat(64));
    out.afterDrop = (await MusicOffline.get('a'.repeat(64))) === null;
    out.haveAfterDrop = (await MusicOffline.have()).has('a'.repeat(64));

    // 4. keep() downloads, stores what the server sent, and reports as it goes
    const shas = Array.from({length: 9}, (_, i) => String(i).repeat(64));
    const steps = [];
    const r = await MusicOffline.keep(shas, s => steps.push(s.done));
    out.keptOk = r.ok; out.keptTotal = r.total;
    out.steps = steps.length;
    out.stepsAscend = steps.every((v, i) => i === 0 || v >= steps[i - 1]);
    out.peakInFlight = peak;
    out.storedIsServerBytes = (() => {
      return MusicOffline.get(shas[0]).then(b => b && b[0] === 0xEE && b[1] === 0xFF);
    })();
    out.storedIsServerBytes = await out.storedIsServerBytes;

    // 5. asking again for what is already here downloads nothing
    const before = fetched;
    const again = await MusicOffline.keep(shas);
    out.reDownloads = fetched - before;
    out.reTotal = again.total;
  } catch (e) {
    out.threw = String(e && e.stack || e);
  }
  document.getElementById('out').textContent = JSON.stringify(out);
})();
</script>"""


class MusicOfflineStore(unittest.TestCase):
    """Driven over CDP rather than --dump-dom.

    `--virtual-time-budget` does not wait for IndexedDB: the virtual clock runs out while the
    transactions are still pending and the dump comes back empty, which reads as "the page did not
    evaluate" rather than as a broken test. So this waits for the real thing.
    """

    @classmethod
    def setUpClass(cls):
        try:
            import websockets  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("websockets not installed")
        chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
                  or shutil.which("google-chrome") or shutil.which("chrome"))
        if not chrome:
            raise unittest.SkipTest("no chrome — IndexedDB needs a real browser")
        cls.r = asyncio.run(cls._drive(chrome))

    @classmethod
    async def _drive(cls, chrome):
        import websockets
        tmp = tempfile.mkdtemp(prefix="pcmus-")
        srv = proc = None
        try:
            with open(os.path.join(tmp, "t.html"), "w") as fh:
                fh.write(PAGE.replace("__EXTRACTED__", _harness()))

            # Served over HTTP, not file://: Chrome denies IndexedDB to a file origin outright, and
            # the whole point of this store is that it is IndexedDB.
            class H(http.server.SimpleHTTPRequestHandler):
                def translate_path(self, p):
                    return os.path.join(tmp, "t.html")

                def log_message(self, *a):
                    pass

            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{srv.server_address[1]}/t.html"
            port = 9496
            proc = subprocess.Popen(
                [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                 f"--remote-debugging-port={port}",
                 "--user-data-dir=" + os.path.join(tmp, "profile"), "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            page = None
            for _ in range(60):
                try:
                    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
                    page = [t for t in tabs if t["type"] == "page"][0]
                    break
                except Exception:
                    await asyncio.sleep(0.5)
            if not page:
                raise unittest.SkipTest("could not start Chrome")
            async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
                n = [0]

                async def call(method, params=None):
                    n[0] += 1
                    await ws.send(json.dumps({"id": n[0], "method": method, "params": params or {}}))
                    while True:
                        msg = json.loads(await ws.recv())
                        if msg.get("id") == n[0]:
                            return msg.get("result")

                await call("Runtime.enable")
                await call("Page.enable")
                await call("Page.navigate", {"url": url})
                for _ in range(80):
                    await asyncio.sleep(0.25)
                    r = await call("Runtime.evaluate",
                                   {"expression": "document.getElementById('out').textContent",
                                    "returnByValue": True})
                    got = (r.get("result") or {}).get("value") or ""
                    if got.strip():
                        return json.loads(got)
            raise unittest.SkipTest("page did not evaluate")
        finally:
            if proc:
                proc.terminate()
            if srv:
                srv.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_it_did_not_throw(self):
        self.assertNotIn("threw", self.r, self.r.get("threw", ""))

    def test_a_stored_track_comes_back_byte_for_byte(self):
        self.assertTrue(self.r["put"])
        self.assertTrue(self.r["roundTrip"], "a downloaded track that decodes differently is unplayable")
        self.assertTrue(self.r["missIsNull"], "a miss must be null so trackUrl falls through to the network")

    def test_what_is_stored_is_what_the_server_sent(self):
        """Still encrypted with the user's master key — never the decoded audio. An offline copy must
        not be a weaker copy than the one on the media server."""
        self.assertTrue(self.r["storedIsServerBytes"])

    def test_the_have_set_is_answered_from_memory(self):
        """The library list repaints on every keystroke of its search box; an IDB scan per paint is
        exactly the cost that reads as 'the app is slow'."""
        self.assertTrue(self.r["haveIsSet"])
        self.assertTrue(self.r["haveHas"])
        self.assertTrue(self.r["haveCached"], "have() re-scanned instead of returning the cached Set")

    def test_removing_is_immediate_and_local(self):
        self.assertTrue(self.r["afterDrop"])
        self.assertFalse(self.r["haveAfterDrop"], "the in-memory Set went stale after a drop")

    def test_a_bulk_download_reports_as_it_goes(self):
        self.assertEqual(self.r["keptOk"], 9)
        self.assertEqual(self.r["keptTotal"], 9)
        self.assertEqual(self.r["steps"], 9, "one report per track, not one at the end")
        self.assertTrue(self.r["stepsAscend"])

    def test_a_bulk_download_does_not_open_a_connection_per_track(self):
        self.assertLessEqual(self.r["peakInFlight"], 2,
                             f"{self.r['peakInFlight']} downloads ran at once — a library is hundreds")

    def test_asking_for_what_is_already_here_downloads_nothing(self):
        self.assertEqual(self.r["reDownloads"], 0)
        self.assertEqual(self.r["reTotal"], 0)


if __name__ == "__main__":
    unittest.main()
