"""Photos -> Synced: every folder of pictures in your synced folders is an album.

Asked 2026-10-28: "maybe photos app should show the albums in the File Manager Synced Folder, like if user syncs photos
back and forth from phone". The shipped client (bundled), with only Folder Sync's account list and record set stood in
-- the same two calls Files makes. A phone's camera roll is synced as `Phone` with DCIM/Camera, Screenshots and a
folder of documents. Photos must list the two picture folders as albums (newest first, counted, the trash and the
documents left out), open one as a grid newest-first with a video marked as one, page a big folder, come back, and
offer Folder Sync when the account syncs nothing. A second half runs files.js's reader in a real page with the real
fetch -> content-hash -> decrypt path, so the thumbnail is the folder's actual picture.
"""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from scripts.client_module_harness import factory_harness_js
from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def _state(camera=3):
    st = {}
    for i in range(camera):
        st[f'DCIM/Camera/IMG_{i:04d}.jpg'] = {'sha': f'{i:064x}', 'size': 2000, 'mtime': 1000 + i}
    st['DCIM/Camera/VID_0001.mp4'] = {'sha': 'f' * 64, 'size': 9000, 'mtime': 5000}
    st['DCIM/Camera/old.jpg'] = {'sha': 'e' * 64, 'size': 1, 'mtime': 1, 'deletedAt': 9}
    st['Screenshots/shot.png'] = {'sha': 'd' * 64, 'size': 100, 'mtime': 900}
    st['Documents/tax.pdf'] = {'sha': 'c' * 64, 'size': 100, 'mtime': 9999}
    st['.pc-trash/2026/x.jpg'] = {'sha': 'b' * 64, 'size': 100, 'mtime': 99999}
    return st


def _stub(state, pairs):
    return ("window.__syncState=%s;window.PCSync=Object.assign({},window.PCSync||{},{acct:()=>%s,folders:()=>[],"
            "accountFolders:async()=>false,docs:{state:async k=>({state:__syncState})}});true"
            % (json.dumps(state), json.dumps(pairs)))


async def _open_synced(b, state, pairs, w=1280, h=900):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": w < 600})
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js(_stub(state, pairs))
    await b.js("__PC.switchView('photos')")
    await b.until("!![...document.querySelectorAll('.ph-tab')].find(t=>t.dataset.tab==='synced')")
    await b.js("[...document.querySelectorAll('.ph-tab')].find(t=>t.dataset.tab==='synced').click()")


@CHROME
@pytest.mark.parametrize('w,h', [(1280, 900), (390, 844)])
def test_picture_folders_become_albums_and_open_newest_first(w, h):
    got = {}

    async def check(b):
        await _open_synced(b, _state(130), [{'key': 'Phone', 'n': 137}], w, h)
        await b.until("document.querySelectorAll('.ph-sync-card').length>0")
        got['albums'] = await b.js("[...document.querySelectorAll('.ph-sync-card')].map(c=>[c.querySelector('b').textContent,c.querySelector('.ph-n').textContent])")
        got['over'] = await b.js("document.documentElement.scrollWidth-innerWidth")
        got['tabs_fit'] = await b.js("(()=>{const t=document.querySelector('.ph-tabs');return t.scrollWidth<=t.clientWidth+1})()")
        await b.js("document.querySelector('.ph-sync-card').click()")
        await b.until("!!document.querySelector('.ph-sync-back')")
        got['title'] = await b.js("document.querySelector('.ph-sync-head h3').textContent")
        got['first'] = await b.js("[...document.querySelectorAll('.ph-grid .ph-tile')].slice(0,3).map(t=>t.title)")
        got['video'] = await b.js("!!document.querySelector('.ph-grid .ph-tile[title=\"VID_0001.mp4\"] .ph-play')")
        got['shown'] = await b.js("document.querySelectorAll('.ph-grid .ph-tile').length")
        await b.js("document.querySelector('.ph-sync-more').click()")
        got['more'] = await b.js("document.querySelectorAll('.ph-grid .ph-tile').length")
        await b.js("document.querySelector('.ph-sync-back').click()")
        got['back'] = await b.js("document.querySelectorAll('.ph-sync-card').length")
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['albums'] == [['Camera', '131'], ['Screenshots', '1']], ("albums", got['albums'])
    assert got['over'] <= 1 and got['tabs_fit'], got
    assert got['title'] == 'Camera' and got['first'][0] == 'VID_0001.mp4' and got['first'][1] == 'IMG_0129.jpg', got
    assert got['video'] and got['shown'] == 120 and got['more'] == 131 and got['back'] == 2, got


@CHROME
def test_no_synced_folders_offers_folder_sync():
    got = {}

    async def check(b):
        await _open_synced(b, {}, [])
        await b.until("!!document.querySelector('.ph-sync-setup')")
        got['text'] = await b.js("document.querySelector('.ph-empty').textContent")
        await b.js("document.querySelector('.ph-sync-setup').click()")
        await asyncio.sleep(.6)
        got['view'] = await b.js("String(__PC.VIEW)")

    asyncio.run(desktop.with_browser('online', '', check))
    assert 'No synced folders yet' in got['text'], got
    assert got['view'] == 'sync', ("Set up Folder Sync did not open Folder Sync", got['view'])


PAGE = r"""<!doctype html><meta charset="utf-8"><body><div id="modal-root"></div><script>
var $ = (s, r) => (r || document).querySelector(s), $$ = (s, r) => [...(r || document).querySelectorAll(s)];
var enc = s => String(s == null ? '' : s); var toast = () => {};
var mimeForName = n => /\.png$/i.test(n) ? 'image/png' : '';
var _standalone = () => false; var mediaServer = () => 'http://media.test';
var FilesIdx = { _ensureMK: async () => 'mk' };
var _masterDecrypt = async (mk, bytes) => bytes.slice(4);
var sha256hex = async buf => [...new Uint8Array(await crypto.subtle.digest('SHA-256', buf))].map(b => b.toString(16).padStart(2, '0')).join('');
var ME = { pubkey: 'ab'.repeat(32) }; var BLOBS = {};
window.fetch = async url => { const h = String(url).split('/').pop().split('?')[0]; return BLOBS[h] ? new Response(BLOBS[h]) : new Response('no', { status: 404 }); };
</script><script src="files.js"></script><script>/*HARNESS*/</script><script>
window.__report = (async () => {
  const R = {};
  try {
    const c = document.createElement('canvas'); c.width = 40; c.height = 20; c.getContext('2d').fillRect(0, 0, 40, 20);
    const png = new Uint8Array(await (await new Promise(r => c.toBlob(r, 'image/png'))).arrayBuffer());
    const sealed = new Uint8Array(png.length + 4); sealed.set(new TextEncoder().encode('ENC:')); sealed.set(png, 4);
    const sha = await sha256hex(sealed.buffer); BLOBS[sha] = sealed;
    window.PCSync = { acct: () => [{ key: 'Phone', n: 2 }], folders: () => [], accountFolders: async () => false,
      docs: { state: async () => ({ state: { 'DCIM/Camera/a.png': { sha, size: sealed.length, mtime: 5 },
                                             'DCIM/Camera/b.mp4': { sha: 'f'.repeat(64), size: 9, mtime: 6 } } }) } };
    const r = await syncedPhotoAlbums(true);
    R.state = r.state; R.albums = r.albums.map(a => [a.key, a.dir, a.n]);
    const url = await syncedPhotoThumb(r.albums[0].cover);
    const im = new Image(); im.src = url; await im.decode(); R.size = [im.naturalWidth, im.naturalHeight];
    R.video = await syncedPhotoThumb(r.albums[0].items[0]);
    window.PCSync.acct = () => 'error';
  } catch (e) { R.error = String(e && e.stack || e); }
  return JSON.stringify(R);
})();
</script>"""


@CHROME
def test_the_thumbnail_is_the_folders_real_picture_decrypted_here():
    files_src = (REPO / 'static/js/client/files.js').read_text(encoding='utf-8')
    harness = factory_harness_js(files_src, 'PCFilesFactory', expose=['syncedPhotoAlbums', 'syncedPhotoThumb'])
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(REPO / 'static/js/client/files.js', os.path.join(tmp, 'files.js'))
        page = os.path.join(tmp, 'p.html')
        Path(page).write_text(PAGE.replace('/*HARNESS*/', harness), encoding='utf-8')
        out = _run_page(tmp, page)
    r = json.loads(out)
    assert 'error' not in r, r
    assert r['state'] == 'ok' and r['albums'] == [['Phone', 'DCIM/Camera', 2]], r
    assert r['size'] == [40, 20], ("the thumbnail is not the decrypted picture", r)
    assert r['video'] is None, "a video was offered a picture thumbnail"


def _run_page(tmp, page):
    """Load the page in headless Chrome and return window.__report (the DM picker test's driver)."""
    import urllib.request
    import websockets
    chrome_bin = shutil.which("google-chrome-stable") or "/opt/google/chrome/chrome"
    prof = os.path.join(tmp, "profile")
    chrome = subprocess.Popen([chrome_bin, "--headless=new", "--disable-gpu", "--no-sandbox", "--remote-debugging-port=0",
                               f"--user-data-dir={prof}", "--allow-file-access-from-files", "about:blank"],
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
                ws_url = next((x["webSocketDebuggerUrl"] for x in pages if x["type"] == "page"), None)
            except OSError:
                pass
            if not ws_url:
                time.sleep(0.1)

        async def ask():
            async with websockets.connect(ws_url, max_size=1 << 24) as ws:
                await ws.send(json.dumps({"id": 5, "method": "Page.navigate", "params": {"url": "file://" + page}}))
                while json.loads(await ws.recv()).get("id") != 5:
                    pass
                await asyncio.sleep(0.5)
                for _ in range(150):
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
        return asyncio.run(asyncio.wait_for(ask(), 90))
    finally:
        try:
            chrome.wait(timeout=15)
        except subprocess.TimeoutExpired:
            chrome.kill()
            chrome.wait()
