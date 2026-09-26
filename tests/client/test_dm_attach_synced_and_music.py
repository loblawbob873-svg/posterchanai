"""Messages can attach a Music track and a file from a Synced Folder.

Run: venv-unified/bin/python -m pytest tests/client/test_dm_attach_synced_and_music.py
     (the picker half needs google-chrome-stable; it skips itself without one)

Reported: "Messages -> Can't add Music or Synced Folder files to Message".

  * Music is an ENCRYPTED folder. The drive picker hides encrypted folders unless the caller says it
    can decrypt what it picks (`allowEncrypted`) — Messages never did, so Music was simply absent,
    and a link to a track would have been ciphertext the recipient cannot open anyway. The pick is
    now decrypted and attached like a file from the device (so 🔒 applies to it).
  * A synced folder was reachable from no attach control. 📎 now offers "From a synced folder",
    which walks the same account-wide pair list, manifest and decrypting fetch the Files screen uses
    (files.js `pickSyncedFile`) and hands back a plain File.

Two halves:
  1. tests/client/dm_attach_sources_runtime.mjs runs the SHIPPED app.js attach code under node.
  2. the picker runs in a real Chrome at PHONE width against the real client.css, with the real
     fetch → content-hash check → chunk join path; only the drive key's decrypt is stubbed.
"""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request

from scripts.client_module_harness import factory_harness_js

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FILES_JS = os.path.join(REPO, "static", "js", "client", "files.js")
CSS = os.path.join(REPO, "static", "css", "client.css")
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")


class AppJsAttachSources(unittest.TestCase):
    def test_the_shipped_attach_code(self):
        r = subprocess.run(["node", os.path.join(REPO, "tests", "client", "dm_attach_sources_runtime.mjs")],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


PAGE = r"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="client.css">
<body><div id="modal-root"></div>
<script>
var $ = (s, r) => (r || document).querySelector(s), $$ = (s, r) => [...(r || document).querySelectorAll(s)];
var enc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
var TOASTS = []; var toast = t => TOASTS.push(String(t));
var _fmtBytes = n => n + ' B';
var mimeForName = n => ({pdf:'application/pdf', txt:'text/plain', mkv:'video/x-matroska'})[String(n).split('.').pop()] || '';
var _standalone = () => false;
var mediaServer = () => 'http://media.test';
var FilesIdx = { _ensureMK: async () => 'mk' };
var _masterDecrypt = async (mk, bytes) => bytes.slice(4);          // "ENC:" + plaintext → plaintext
var sha256hex = async buf => [...new Uint8Array(await crypto.subtle.digest('SHA-256', buf))]
                              .map(b => b.toString(16).padStart(2, '0')).join('');
var ME = { pubkey: 'ab'.repeat(32) };
var BLOBS = {};
async function seal(text){ const b = new TextEncoder().encode('ENC:' + text); const h = await sha256hex(b.buffer); BLOBS[h] = b; return h; }
window.fetch = async url => { const h = String(url).split('/').pop().split('?')[0];
  return BLOBS[h] ? new Response(BLOBS[h]) : new Response('no', { status: 404 }); };
</script>
<script src="files.js"></script>
<script>/*HARNESS*/</script>
<script>
const until = async (fn, ms = 5000) => { const t = Date.now(); for(;;){ const v = fn(); if(v) return v;
  if(Date.now() - t > ms) throw new Error('timed out waiting'); await new Promise(r => setTimeout(r, 20)); } };
const rows = () => $$('.sp-row .sp-name').map(e => e.textContent);
const click = name => $$('.sp-row').find(b => b.querySelector('.sp-name').textContent === name).click();
window.__report = (async () => {
  const R = {};
  try {
    const A = await seal('alpha'), C1 = await seal('part-one|'), C2 = await seal('part-two');
    window.PCSync = { acct: () => [{ key: 'Docs', n: 4 }], folders: () => [],
      docs: { state: async key => ({ state: {
        'a.txt': { sha: A, size: 5 }, 'sub/b.pdf': { chunks: [C1, C2], size: 17 },
        'gone.txt': { sha: A, size: 5, deletedAt: 9 }, 'big.mkv': { sha: A, size: 600 * 1024 * 1024 },
      } }) } };

    // 1. walk, refuse the oversized file, go back, pick the chunked PDF
    let p = pickSyncedFile();
    await until(() => rows().length);
    R.roots = rows();
    const box = $('.sp-picker').getBoundingClientRect();
    R.fits = { w: box.width, vw: innerWidth, left: box.left };
    R.rowH = Math.min(...$$('.sp-row').map(b => b.getBoundingClientRect().height));
    click('Docs'); await until(() => rows().includes('a.txt'));
    R.docs = rows();
    click('big.mkv'); await new Promise(r => setTimeout(r, 50));
    R.bigRefused = TOASTS.some(t => /too big/.test(t)) && !!$('.sp-picker');
    click('sub'); await until(() => rows().includes('b.pdf'));
    R.sub = rows();
    $('.sp-up').click(); await until(() => rows().includes('a.txt'));
    R.backWorks = rows().includes('sub');
    click('sub'); await until(() => rows().includes('b.pdf'));
    click('b.pdf');
    const f = await p;
    R.picked = f && { name: f.name, type: f.type, text: await f.text() };
    R.closed = !$('.sp-picker') && !document.body.classList.contains('modal-open');

    // 2. Escape cancels
    p = pickSyncedFile(); await until(() => rows().length);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    R.escaped = await p;
    R.closedAfterEscape = !$('.sp-picker');

    // 3. an account with no synced folders says so
    window.PCSync.acct = () => [];
    p = pickSyncedFile(); await until(() => $('.sp-list .empty'));
    R.emptyText = $('.sp-list .empty').textContent;
    $('.sp-close').click(); R.emptyCancel = await p;
  } catch (e) { R.error = String(e && e.stack || e) + ' | reached: ' + Object.keys(R).join(','); }
  return JSON.stringify(R);
})();
</script>
"""


def _run_page():
    import websockets
    with open(FILES_JS, encoding="utf-8") as fh:
        files_src = fh.read()
    harness = factory_harness_js(files_src, "PCFilesFactory", expose=["pickSyncedFile"])
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(FILES_JS, os.path.join(tmp, "files.js"))
        shutil.copy(CSS, os.path.join(tmp, "client.css"))
        page = os.path.join(tmp, "picker.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(PAGE.replace("/*HARNESS*/", harness))
        prof = os.path.join(tmp, "profile")
        chrome = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--remote-debugging-port=0",
             f"--user-data-dir={prof}", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port_file, deadline = os.path.join(prof, "DevToolsActivePort"), time.time() + 30
            while not os.path.exists(port_file) and time.time() < deadline:
                time.sleep(0.1)
            port = open(port_file).read().split()[0]
            ws_url = None
            while not ws_url and time.time() < deadline:
                try:
                    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                    ws_url = next((p["webSocketDebuggerUrl"] for p in pages
                                   if p["type"] == "page"), None)
                except OSError:
                    pass
                if not ws_url:
                    time.sleep(0.1)

            async def ask():
                async with websockets.connect(ws_url, max_size=1 << 24) as ws:
                    n = 10
                    for method, params in (
                            ("Emulation.setDeviceMetricsOverride",
                             {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True}),
                            ("Page.navigate", {"url": "file://" + page})):
                        n += 1
                        await ws.send(json.dumps({"id": n, "method": method, "params": params}))
                        while json.loads(await ws.recv()).get("id") != n:
                            pass
                    await asyncio.sleep(0.5)
                    for _ in range(100):
                        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                            "expression": "window.__report || null", "awaitPromise": True, "returnByValue": True}}))
                        while True:
                            r = json.loads(await ws.recv())
                            if r.get("id") == 1:
                                break
                        v = r.get("result", {}).get("result", {}).get("value")
                        if v:
                            await ws.send(json.dumps({"id": 2, "method": "Browser.close"}))
                            return v
                        await asyncio.sleep(0.1)
                    raise AssertionError(f"the page never reported: {r}")
            got = json.loads(asyncio.run(asyncio.wait_for(ask(), 90)))
        finally:
            try:
                chrome.wait(timeout=15)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait()
    assert "error" not in got, got["error"]
    return got


@unittest.skipUnless(CHROME, "needs Chrome")
class SyncedFolderPicker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = _run_page()

    def test_lists_the_accounts_synced_folders(self):
        self.assertEqual(self.r["roots"], ["Docs"])

    def test_a_folder_lists_dirs_then_files_and_never_a_deleted_file(self):
        self.assertEqual(self.r["docs"], ["sub", "a.txt", "big.mkv"])
        self.assertNotIn("gone.txt", self.r["docs"], "a tombstone is not a file")
        self.assertEqual(self.r["sub"], ["b.pdf"])
        self.assertTrue(self.r["backWorks"])

    def test_picking_a_chunked_file_gives_its_real_bytes(self):
        self.assertEqual(self.r["picked"], {"name": "b.pdf", "type": "application/pdf",
                                            "text": "part-one|part-two"})
        self.assertTrue(self.r["closed"], "the picker must close and release body.modal-open")

    def test_an_oversized_file_is_refused_and_the_picker_stays(self):
        self.assertTrue(self.r["bigRefused"])

    def test_escape_and_close_cancel_with_nothing(self):
        self.assertIsNone(self.r["escaped"])
        self.assertTrue(self.r["closedAfterEscape"])
        self.assertIsNone(self.r["emptyCancel"])
        self.assertIn("No synced folders", self.r["emptyText"])

    def test_it_fits_a_phone(self):
        fit = self.r["fits"]
        self.assertLessEqual(fit["w"], fit["vw"] + 0.5, fit)
        self.assertGreaterEqual(fit["left"], -0.5, fit)
        self.assertGreaterEqual(self.r["rowH"], 44, "rows must be finger-sized on a phone")


if __name__ == "__main__":
    unittest.main()
