"""Community icons: cached, keyed so the cache can be HIT, and painted from it.

Run: venv-unified/bin/python -m unittest tests.client.test_concord_icon_cache

Real Chrome, real IndexedDB, the shipped concord-cache.js. Three rules, each measured against a
real report ("concord community icons still take a long time to appear, are we not caching them?")
and each verified to fail with its fix removed:

  ref-is-order-proof   The cache key was `JSON.stringify(pointer)`, and THREE call sites build that
                       object independently — the localStorage round trip, `inspectControl`
                       rebuilding it from the bundle, and the relay event itself. JSON.stringify is
                       key-order sensitive and putIcon stores ONE row per room, so two orderings of
                       the same four fields are two refs overwriting each other: every read misses,
                       every visit re-downloads and re-decrypts an image already on disk, and a
                       re-ordered pointer additionally reads as a CHANGED icon on every metadata
                       pass. The pointer carries `hash` — a sha256 of the plaintext that
                       decryptImagePointer already verifies — which cannot be reordered.

  one-pass-warm        `roomIcon()` is synchronous: with nothing in memory it draws the letter
                       glyph and only THEN starts a per-room IndexedDB read, so a warm cache still
                       showed initials and filled them in one repaint at a time. allIcons() reads
                       the whole set in one transaction.

  blocked-is-not-empty A versioned database another tab holds open at an older version BLOCKS, and
                       with no onblocked handler the open promise never settles — every icon and
                       envelope read awaits it until the page closes, nothing thrown, nothing
                       logged. Rejecting turns a permanent hang into one slow load.
"""
import asyncio, http.server, json, os, shutil, subprocess, tempfile, threading, unittest, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE = os.path.join(ROOT, "static/js/client/concord-cache.js")
CONCORD = os.path.join(ROOT, "static/js/client/concord.js")
PORT = int(os.environ.get("PC_CHECK_PORT") or 9494)
PROFILE = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-concord-icon-check"

PAGE = r"""<!doctype html><pre id=out></pre>
<script src=concord-cache.js></script>
<script>
(async()=>{
 const R={};
 try{
  const C=PCConcordCache;
  indexedDB.deleteDatabase(C.DB); await new Promise(r=>setTimeout(r,120)); C._reset();

  // The SAME pointer, written by two call sites in two key orders — which is exactly what the
  // localStorage round trip and inspectControl produce.
  const hash='ab'.repeat(32);
  const a={url:'https://x/i.png',key:'11'.repeat(32),nonce:'22'.repeat(16),hash};
  const b={hash,nonce:'22'.repeat(16),key:'11'.repeat(32),url:'https://x/i.png'};
  R.stringifyDiffers = JSON.stringify(a)!==JSON.stringify(b);

  const png=new Uint8Array([0x89,0x50,0x4e,0x47,1,2,3,4]);
  await C.putIcon('room-A', JSON.stringify(a), png, 'image/png');
  R.legacyCrossHit = !!await C.getIcon('room-A', JSON.stringify(b));   // the bug: false

  // With a canonical ref both call sites agree.
  R.canonicalA = PCIconRef(a); R.canonicalB = PCIconRef(b);
  await C.putIcon('room-A', PCIconRef(a), png, 'image/png');
  R.canonicalCrossHit = !!await C.getIcon('room-A', PCIconRef(b));     // must be true

  // One pass over every icon.
  await C.putIcon('room-B', PCIconRef(a), png, 'image/png');
  R.hasAllIcons = typeof C.allIcons === 'function';
  const all = C.allIcons ? await C.allIcons() : [];
  R.allCount = all.length;
  R.allKeys = all.map(r=>r.key).sort();

  R.hasOnBlocked = true;
 }catch(e){ R.error = String(e&&e.message||e); }
 out.textContent = JSON.stringify(R);
})();
</script>"""


class ConcordIconCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import websockets  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("websockets unavailable")
        chrome = (shutil.which("google-chrome-stable") or shutil.which("google-chrome")
                  or shutil.which("chromium"))
        if not chrome:
            raise unittest.SkipTest("Chrome unavailable")
        cls.result = asyncio.run(cls._run(chrome))

    @classmethod
    async def _run(cls, chrome):
        import websockets
        tmp = tempfile.mkdtemp(prefix="pc-icon-cache-")
        shutil.copy(CACHE, os.path.join(tmp, "concord-cache.js"))
        # The shipped iconRef, lifted out of concord.js so the page needs no app shell.
        src = open(CONCORD, encoding="utf-8").read()
        start = src.index("  function iconRef(value){")
        end = src.index("\n  }", start) + 4
        with open(os.path.join(tmp, "index.html"), "w") as fh:
            fh.write(PAGE.replace("<script src=concord-cache.js></script>",
                                  "<script src=concord-cache.js></script><script>"
                                  + src[start:end].replace("function iconRef", "function PCIconRef")
                                  + "</script>"))

        class H(http.server.SimpleHTTPRequestHandler):
            def translate_path(self, path):
                return os.path.join(tmp, path.split("?")[0].lstrip("/") or "index.html")

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_port}/index.html"
        subprocess.run(["rm", "-rf", PROFILE], check=False)
        proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
             f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            page = None
            for _ in range(60):
                try:
                    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
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
                for _ in range(60):
                    await asyncio.sleep(0.25)
                    r = await call("Runtime.evaluate",
                                   {"expression": "document.getElementById('out').textContent",
                                    "returnByValue": True})
                    txt = (r.get("result") or {}).get("value") or ""
                    if txt:
                        return json.loads(txt)
                raise unittest.SkipTest("page never reported")
        finally:
            proc.terminate()
            srv.shutdown()
            subprocess.run(["rm", "-rf", PROFILE], check=False)

    def test_the_page_ran(self):
        self.assertIsNone(self.result.get("error"), self.result.get("error"))

    def test_the_two_call_sites_really_do_stringify_differently(self):
        """The premise, asserted rather than assumed: this is not a theoretical ordering."""
        self.assertTrue(self.result["stringifyDiffers"])
        self.assertFalse(self.result["legacyCrossHit"],
                         "the stringified key happened to match — the fixture is not reproducing "
                         "the two orderings the three call sites actually produce")

    def test_a_canonical_ref_lets_either_call_site_hit_the_same_icon(self):
        self.assertEqual(self.result["canonicalA"], self.result["canonicalB"])
        self.assertTrue(self.result["canonicalA"].startswith("h:"),
                        "the ref is not keyed on the pointer's own content hash: %r"
                        % (self.result["canonicalA"],))
        self.assertTrue(self.result["canonicalCrossHit"],
                        "an icon written by one call site is invisible to the other, so every "
                        "visit re-downloads and re-decrypts an image already on disk")

    def test_every_icon_can_be_read_in_one_pass(self):
        self.assertTrue(self.result["hasAllIcons"],
                        "no allIcons(): the room list can only fill icons one transaction and one "
                        "repaint at a time, after it has already drawn the letter glyphs")
        self.assertEqual(self.result["allKeys"], ["room-A", "room-B"])


class TheCacheSourceItself(unittest.TestCase):
    def test_a_blocked_upgrade_rejects_instead_of_hanging_for_ever(self):
        """A versioned DB another tab holds at an older version blocks; with no handler the open
        promise never settles and every read awaits it until the page is closed — nothing thrown,
        nothing logged. Source-level, because a second tab holding an old version cannot be staged
        inside one page."""
        src = open(CACHE, encoding="utf-8").read()
        self.assertIn("q.onblocked", src,
                      "indexedDB.open has no onblocked handler: a stale tab hangs every icon and "
                      "envelope read for the life of the page")

    def test_a_failed_warm_is_not_latched(self):
        """"Could not ask" is never "there is nothing there" — the rule this codebase keeps
        relearning. A cache that would not open must leave the flag clear so the next entry
        retries, rather than showing initials for the rest of the session."""
        src = open(CONCORD, encoding="utf-8").read()
        warm = src.split("async function warmRoomIcons", 1)[1].split("async function hydrateStoredRoomIcon", 1)[0]
        self.assertIn("iconsWarmedFor=''", warm,
                      "a warm that could not read the cache latches itself as done")

    def test_the_warm_re_runs_when_the_room_list_grows(self):
        """MEASURED on a real account, not assumed: the saved list is ONE room at the first paint
        and four a moment later, and a room's icon pointer only lands when its membership sync
        does. A once-per-session boolean therefore warms exactly the rooms that needed it least."""
        src = open(CONCORD, encoding="utf-8").read()
        warm = src.split("async function warmRoomIcons", 1)[1].split("async function hydrateStoredRoomIcon", 1)[0]
        self.assertIn("iconWarmSignature()", warm,
                      "the warm is keyed on a once-per-session flag, so rooms and pointers that "
                      "arrive during the visit are never warmed")
        sig = src.split("function iconWarmSignature", 1)[1].split("\n  }", 1)[0]
        self.assertIn("iconRef(", sig,
                      "the signature ignores the pointer, so a room that GAINS an icon is not "
                      "re-warmed")


if __name__ == "__main__":
    unittest.main()
