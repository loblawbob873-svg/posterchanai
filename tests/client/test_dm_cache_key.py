"""The decrypted-DM cache: one signer call per session, and never a second key.

WHY IT EXISTS. Measured on the relay while three clients looked stuck: 2769 kind-24133 in 110
seconds, requests and replies 1:1 in both directions (663 of 664, 655 of 655). Nothing was dropped
and nothing was deaf — the phone answered everything. One message is TWO decrypts, a 400-message
history is 800 round trips, and the client threw every result away on reload, so all three devices
paid it again on every visit ("stuck at 1/400", "not decrypting anything", "worse than amber").

So the plaintext is cached, under a key wrapped to the user — the encrypted drive's pattern, which
is already proven here: one `pcai:dmkey` doc, one nip44dec per session, everything else local AES.

WHAT THIS TEST GUARDS, because each of these loses something and none of them says so:

  one-unwrap        400 messages must cost ONE signer call, not 400. That is the entire feature.
  never-a-2nd-key   a SILENT relay must not mint a new key over the existing one. That is the
                    replaceable-doc wipe this codebase has paid for repeatedly — and here it would
                    make every cached message on every device unreadable at once.
  undecryptable     …and neither may a doc that is there but will not decrypt (a signer that timed
                    out mid-read). Same wipe, different cause.
  fails-as-a-cache  no key, no IndexedDB, a corrupt record: every one falls through to the signer
                    rather than throwing. A cache that can break the app is not worth having.
  one-failure-once  a dead signer must not be asked once PER MESSAGE — 400 wraps calling get()
                    would be 400 more requests at the thing that is already struggling.

The module is EXTRACTED from app.js, not copied, so it cannot drift from what ships.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "static", "js", "client", "app.js")


def _module():
    with open(APP) as fh:
        src = fh.read()
    i = src.index("  const DmCache = {")
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1] + ";"
        j += 1
    raise AssertionError("could not bound DmCache")


PAGE = """<!doctype html><meta charset="utf-8"><pre id="out"></pre><script>
const sleep = ms => new Promise(r=>setTimeout(r,ms));
const out = {};

// --- the world DmCache lives in, reduced to what it touches -------------------------------------
let ME = { pubkey: 'ab'.repeat(32) };
let decs = 0, encs = 0, published = [], relayAnswers = true, docs = [], decThrows = false;
const KEYHEX = '11'.repeat(32);

const signer = {
  async nip44dec(peer, ct){ decs++; if (decThrows) throw new Error('signer timed out'); return ct; },
  async nip44enc(peer, text){ encs++; return text; },
};
const Relay = {
  // HONOURS `#d`, because the module asks for two DIFFERENT documents (the key and the shared-cache
  // pointer) and a stub that returns everything hands each read the other one's content. That is not
  // a hypothetical: it made pushShared parse a 32-byte key as a pointer and answer false.
  async query(f){
    if (!relayAnswers) throw new Error('no relay answered');
    const want = (f && f[0] && f[0]['#d'] && f[0]['#d'][0]) || null;
    if (!want) return docs.slice();
    return docs.filter(d => (d.tags || []).some(t => t[0] === 'd' && t[1] === want));
  },
};
const publish = async (kind, content, tags) => { published.push({kind, content, tags}); return {ok:true}; };
let blobs = {}, uploaded = 0, mediaOk = true;
const mediaServer = () => mediaOk ? 'https://blossom.test' : '';
const uploadBlob = async (file) => {
  uploaded++;
  const sha = 'ab'.repeat(31) + (Object.keys(blobs).length).toString(16).padStart(2,'0');
  blobs[sha] = new Uint8Array(await file.arrayBuffer());
  return 'https://blossom.test/' + sha;
};
const _shaFromUrl = (u) => (String(u).match(/([0-9a-f]{64})/i) || [,''])[1];
const _origFetch = window.fetch;
window.fetch = async (u) => {
  const sha = _shaFromUrl(u);
  if (sha && blobs[sha]) return { ok:true, arrayBuffer: async () => blobs[sha].buffer };
  return { ok:false, arrayBuffer: async () => new ArrayBuffer(0) };
};

/* IndexedDB, in memory. NOT because the real one is uninteresting, but because headless Chrome's
 * --virtual-time-budget and IndexedDB do not cooperate: the page never settles and the whole test
 * skips with "page did not evaluate", which is a test that proves nothing while looking present.
 * The wrapper around it is four lines; everything this file is actually about — one unwrap, never a
 * second key, the failure backoff — is above that layer. WebCrypto is REAL (hence the http://
 * origin: a file:// page is not a secure context and Chromium deletes crypto.subtle). */
const _mem = new Map();
// defineProperty, not assignment: `indexedDB` is a READ-ONLY getter on Window, so
// `window.indexedDB = shim` fails silently in sloppy mode and the page then hangs on the
// real one under virtual time — which is exactly the empty-<pre> skip this test hit twice.
Object.defineProperty(window, 'indexedDB', { configurable: true, value: {
  open(){
    const req = {};
    const store = {
      get(k){ const q={}; setTimeout(()=>{ q.result = _mem.get(k) || null; q.onsuccess && q.onsuccess(); },0); return q; },
      put(v,k){ const q={}; setTimeout(()=>{ _mem.set(k,v); q.onsuccess && q.onsuccess(); },0); return q; },
      count(){ const q={}; setTimeout(()=>{ q.result=_mem.size; q.onsuccess && q.onsuccess(); },0); return q; },
      // pushShared() walks the whole store to build the blob, so the shim needs a cursor. Without
      // it the export threw into its own catch and answered a perfectly quiet `false`.
      openCursor(){
        const q={}; const keys=[..._mem.keys()]; let i=0;
        const step=()=>{ setTimeout(()=>{
          if(i>=keys.length){ q.result=null; q.onsuccess && q.onsuccess(); return; }
          const k=keys[i++];
          q.result={ key:k, value:_mem.get(k), continue:step };
          q.onsuccess && q.onsuccess();
        },0); };
        step(); return q;
      },
      clear(){ _mem.clear(); },
    };
    const db = { transaction(){ return { objectStore(){ return store; } }; },
                 createObjectStore(){ return store; } };
    setTimeout(()=>{ req.result = db; req.onsuccess && req.onsuccess(); }, 0);
    return req;
  },
  deleteDatabase(){ _mem.clear(); return {}; },
} });

__MODULE__

(async () => {
  // 1. ONE unwrap for a whole history. 400 gets, one nip44dec.
  {
    docs = [{ content: KEYHEX, created_at: 1, tags:[['d','pcai:dmkey']] }];
    decs = 0;
    const rumor = { kind:14, pubkey:'cd'.repeat(32), content:'hello', tags:[], created_at:5 };
    await DmCache.put('wrap1', rumor);
    const got = [];
    for (let i = 0; i < 400; i++) got.push(await DmCache.get('wrap1'));
    out.oneUnwrapDecs = decs;
    out.roundTripped = JSON.stringify(got[0]) === JSON.stringify(rumor);
    out.allHit = got.every(g => g && g.content === 'hello');
    out.missIsNull = (await DmCache.get('never-seen')) === null;
  }

  // 2. A SILENT relay must not mint a second key.
  {
    DmCache.forget(); await sleep(30);
    relayAnswers = false; published = []; encs = 0;
    const k = await DmCache.get('wrap1');
    out.silentMinted = published.length;
    out.silentReturned = k;
    relayAnswers = true;
  }

  // 3. A doc that will not decrypt must not be replaced either.
  {
    DmCache.forget(); await sleep(30);
    DmCache._offUntil = 0;
    docs = [{ content:'garbage', created_at:1, tags:[['d','pcai:dmkey']] }];
    decThrows = true; published = [];
    await DmCache.get('wrap1');
    out.undecryptableMinted = published.length;
    decThrows = false;
  }

  // 4. A first run with NO doc mints exactly one, and publishes it.
  {
    DmCache.forget(); await sleep(30);
    DmCache._offUntil = 0;
    docs = []; published = []; encs = 0;
    await DmCache.put('wrap2', { kind:14, pubkey:'ef'.repeat(32), content:'hi', tags:[], created_at:6 });
    out.freshPublished = published.length;
    out.freshTag = published[0] && published[0].tags[0][1];
    out.freshKind = published[0] && published[0].kind;
  }

  // 5. A dead signer is asked ONCE, not once per message.
  {
    DmCache.forget(); await sleep(30);
    DmCache._offUntil = 0;
    docs = [{ content: KEYHEX, created_at:1, tags:[['d','pcai:dmkey']] }];
    decThrows = true; decs = 0;
    for (let i = 0; i < 50; i++) await DmCache.get('wrap' + i);
    out.deadSignerDecs = decs;
    decThrows = false;
  }

  // 6. THE SHARED CACHE. One device exports; a second device with an EMPTY cache imports the whole
  //    history with no signer round trips at all beyond the one key unwrap.
  {
    DmCache.forget(); await sleep(30); DmCache._offUntil = 0;
    docs = [{ content: KEYHEX, created_at:1, tags:[['d','pcai:dmkey']] }];
    published = []; blobs = {}; uploaded = 0;
    for (let i = 0; i < 5; i++)
      await DmCache.put('w'+i, { kind:14, pubkey:'cd'.repeat(32), content:'m'+i, tags:[], created_at:i });
    DmCache._pushedAt = 0;
    out.pushed = await DmCache.pushShared();
    const ptr = published.find(p => p.tags && p.tags[0] && p.tags[0][1] === 'pcai:dmcache');
    out.ptrPublished = !!ptr;
    out.ptrN = ptr ? JSON.parse(ptr.content).n : null;
    out.uploaded = uploaded;
    if (!ptr) { document.getElementById('out').textContent = JSON.stringify(out); return; }
    // …a fresh device: same key doc, same relay, empty local store.
    const carry = JSON.parse(ptr.content);
    DmCache.forget(); await sleep(30); DmCache._offUntil = 0;
    docs = [{ content: KEYHEX, created_at:1, tags:[['d','pcai:dmkey']] },
            { content: JSON.stringify(carry), created_at:2, tags:[['d','pcai:dmcache']] }];
    decs = 0;
    out.imported = await DmCache.pullShared();
    out.importDecs = decs;                       // the KEY unwrap and nothing else
    out.importedHit = (await DmCache.get('w3')).content === 'm3';
  }

  // 7. A SMALLER cache must not replace a bigger published one.
  {
    DmCache.forget(); await sleep(30); DmCache._offUntil = 0;
    docs = [{ content: KEYHEX, created_at:1, tags:[['d','pcai:dmkey']] },
            { content: JSON.stringify({sha:'cd'.repeat(32), n:900, at:1}), created_at:2,
              tags:[['d','pcai:dmcache']] }];
    published = [];
    await DmCache.put('only', { kind:14, pubkey:'cd'.repeat(32), content:'x', tags:[], created_at:1 });
    DmCache._pushedAt = 0;
    out.shrinkPushed = await DmCache.pushShared();
    out.shrinkPublished = published.filter(p => p.tags[0][1] === 'pcai:dmcache').length;
  }

  document.getElementById('out').textContent = JSON.stringify(out);
})().catch(e => { document.getElementById('out').textContent = JSON.stringify({err:String(e)}); });
</script>"""


class DmCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
                  or shutil.which("google-chrome") or shutil.which("chrome"))
        if not chrome:
            raise unittest.SkipTest("no chrome")
        tmp = tempfile.mkdtemp(prefix="pcdmc-")
        srv = None
        try:
            path = os.path.join(tmp, "t.html")
            with open(path, "w") as fh:
                fh.write(PAGE.replace("__MODULE__", _module()))
# SERVED OVER http://127.0.0.1, NEVER file://. A file page is not a secure context, so Chromium
            # deletes `crypto.subtle` — and this module is AES-GCM plus IndexedDB, both of which need
            # one. The first run of this test skipped with "page did not evaluate" for exactly that,
            # which is the same trap the desktop build hit (see desktop/main.js: the bundle is loaded
            # over app://, not file://, for this reason).
            class _Q(SimpleHTTPRequestHandler):
                def __init__(self, *a, **k): super().__init__(*a, directory=tmp, **k)
                def log_message(self, *a): pass
            srv = ThreadingHTTPServer(("127.0.0.1", 0), _Q)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            res = subprocess.run(
                [chrome, "--headless", "--no-sandbox", "--disable-gpu",
                 "--virtual-time-budget=20000", "--dump-dom",
                 f"http://127.0.0.1:{srv.server_address[1]}/t.html"],
                capture_output=True, text=True, timeout=180).stdout
            m = re.search(r'<pre id="out">(.*?)</pre>', res, re.S)
            if not m or not m.group(1).strip():
                raise unittest.SkipTest("page did not evaluate")
            cls.r = json.loads(m.group(1))
            assert "err" not in cls.r, cls.r["err"]
        finally:
            if srv: srv.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_whole_history_costs_one_signer_call(self):
        """THE FEATURE. 400 reads, one nip44dec — where the uncached path is two per message."""
        self.assertEqual(self.r["oneUnwrapDecs"], 1,
                         "the key is being unwrapped more than once per session")
        self.assertTrue(self.r["allHit"], "cached messages did not come back")
        self.assertTrue(self.r["roundTripped"], "the cached rumor is not byte-identical")

    def test_a_miss_is_null_not_an_error(self):
        """A cache miss is the ordinary case (every message, the first time). It must be a null the
        caller falls through on, never a throw that takes the restore down with it."""
        self.assertTrue(self.r["missIsNull"], "a miss did not answer null")

    def test_a_silent_relay_never_mints_a_second_key(self):
        """"Nobody answered" is not "you have no key". Publishing one here replaces the real key and
        every cached message on every device becomes unreadable at once — the replaceable-doc wipe,
        with a blast radius bigger than any of the previous ones."""
        self.assertEqual(self.r["silentMinted"], 0, "a new key was published over the existing one")
        self.assertIsNone(self.r["silentReturned"], "it must fall through to the signer instead")

    def test_a_doc_that_will_not_decrypt_is_not_replaced(self):
        """The other way to read nothing: the doc is there and the signer timed out mid-read."""
        self.assertEqual(self.r["undecryptableMinted"], 0)

    def test_a_first_run_mints_exactly_one_key_and_stores_it(self):
        self.assertEqual(self.r["freshPublished"], 1)
        self.assertEqual(self.r["freshTag"], "pcai:dmkey")
        self.assertEqual(self.r["freshKind"], 30078)

    def test_a_fresh_device_imports_the_whole_history_with_no_round_trips(self):
        """The point of the shared cache: three clients used to mean three separate 900-round-trip
        passes at one phone ("tablet fast, windows slow, firefox 1/400"). One device does it; the
        rest read one relay doc and one encrypted blob."""
        self.assertIs(self.r["pushed"], True, "the working device never published its cache")
        self.assertTrue(self.r["ptrPublished"], "no pcai:dmcache pointer was written")
        self.assertEqual(self.r["ptrN"], 5)
        self.assertEqual(self.r["imported"], 5, "the fresh device imported nothing")
        self.assertEqual(self.r["importDecs"], 1,
                         "importing cost more than the one key unwrap")
        self.assertTrue(self.r["importedHit"], "an imported message did not read back")

    def test_a_smaller_cache_never_replaces_a_bigger_published_one(self):
        """The same rule as the drive index and the folder-sync manifest: a partial or empty local
        state must not become the truth for every other device."""
        self.assertIs(self.r["shrinkPushed"], False)
        self.assertEqual(self.r["shrinkPublished"], 0, "it published over a bigger cache")

    def test_a_dead_signer_is_asked_once_not_once_per_message(self):
        """400 wraps calling get() against a signer that is down would be 400 more requests at the
        thing that is already struggling — which is the failure this whole cache exists to end."""
        self.assertLessEqual(self.r["deadSignerDecs"], 2,
                             f"{self.r['deadSignerDecs']} signer calls for 50 cache misses")


if __name__ == "__main__":
    unittest.main()
