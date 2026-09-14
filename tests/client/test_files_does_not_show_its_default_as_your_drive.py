"""AN INDEX THAT HAS NOT LOADED IS NOT A DRIVE WITH ONE FOLDER IN IT.

Reported as "webui showing Music and posts only in Files", then "i had to click again to see all my
blossom folders". Nothing was lost. Two faults stacked into something that looks exactly like
deletion:

  1. `FilesIdx` starts holding its own DEFAULT — `folders: ['Music']` — and `_fxSideHTML` drew that
     default as though it were the answer. An empty answer dressed as an answer, which is the
     failure this codebase keeps re-learning.
  2. Nothing repainted when the real index arrived. The pull happens once per session and the ONLY
     thing that redrew this screen was the manual Refresh button, so the default stayed on screen
     until the user clicked something else.

Either alone is survivable. Together they mean: open Files early, see one folder, conclude your
drive has been wiped.

THE RULES
  says-loading   while the list is still the built-in default and the pull has not finished, the
                 sidebar says so instead of drawing a folder list it does not have
  repaints-itself when the index lands the folders appear with NO further interaction
  keeps-a-warm-list a client that already holds real folders keeps showing them while it refreshes —
                 the guard is on the DEFAULT, never on "_pullDone is false", or every warm reopen
                 would blank a drive it can already see

Drives the real client document and the shipped renderers.
"""
import asyncio
import shutil
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler, INIT

# A drive that takes a beat to arrive, then brings four folders with it.
SLOW_DRIVE = """(() => {
  const F = __PC.filesIdx();
  F.data = { folders: ['Music'], files: {}, encFolders: [] };
  F._norm(); F._pullDone = false; F._pullOk = false;
  F.ensure = () => new Promise(res => setTimeout(() => {
    F.data = { folders: ['Music', 'Backgrounds', 'Memes', 'Notes'], files: {}, encFolders: [] };
    F._norm(); F._pullDone = true; F._pullOk = true; res(F.data);
  }, 700));
  return true;
})()"""

WARM_DRIVE = """(() => {
  const F = __PC.filesIdx();
  F.data = { folders: ['Music', 'Backgrounds', 'Memes'], files: {}, encFolders: [] };
  F._norm(); F._pullDone = false; F._pullOk = false;   // still refreshing, but we HAVE a list
  F.ensure = () => new Promise(res => setTimeout(() => { F._pullDone = true; res(F.data); }, 400));
  return true;
})()"""

READ = """(() => {
  const chips = [...document.querySelectorAll('.folder-chip')].map(b => (b.dataset.folder || '')).filter(Boolean);
  return { chips, loading: !!document.querySelector('#fx-folders-loading'),
           text: (document.querySelector('.fx-side') || {}).innerText || '' };
})()"""


async def run(seed):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix='pc-files-default-')
    try:
        proc = subprocess.Popen(
            ['/opt/google/chrome/chrome', '--headless=new', '--no-sandbox', '--disable-gpu',
             '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, 'DevToolsActivePort').exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, 'DevToolsActivePort').read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get('http://127.0.0.1:' + port + '/json')).json()
            tgt = next(p for p in pages if p.get('type') == 'page' and p.get('url') == 'about:blank')
            async with websockets.connect(tgt['webSocketDebuggerUrl'], max_size=20_000_000) as ws:
                b = Browser(ws)
                await b.call('Page.enable')
                await b.call('Network.enable')
                await b.call('Network.setBlockedURLs', {'urls': ['https://*', 'wss://*']})
                await b.call('Emulation.setDeviceMetricsOverride',
                             {'width': 1280, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
                await b.call('Page.addScriptToEvaluateOnNewDocument',
                             {'source': 'window.__hasChats=false;' + INIT})
                await b.call('Page.navigate',
                             {'url': 'http://127.0.0.1:%d/client' % server.server_port})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("""(()=>{const key=new Uint8Array(32).fill(5);
                  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);
                  document.querySelector('#btn-nsec-login').click()})()""")
                await b.until("!!__PC.me()")
                await b.js(seed)
                await b.js("__PC.switchView('blossom')")
                await b.until("!!document.querySelector('.fx-side')")
                await asyncio.sleep(0.25)
                early = await b.js(READ)
                await asyncio.sleep(1.6)          # the index lands; NOBODY clicks anything
                late = await b.js(READ)
                return early, late
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    finally:
        shutil.rmtree(profile, ignore_errors=True)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='no Chrome')
def test_a_cold_drive_says_loading_then_fills_itself_in():
    early, late = asyncio.run(run(SLOW_DRIVE))
    print('early=%r late=%r' % (early, late))

    assert early['loading'], (
        'Files drew a folder list before the index had loaded. What it drew is the built-in '
        'default, so the screen claims the drive holds one folder: %r' % (early['chips'],))
    assert 'Music' not in early['chips'], (
        'the default folder was presented as the answer: %r' % (early['chips'],))

    for want in ('Backgrounds', 'Memes', 'Notes'):
        assert want in late['chips'], (
            'the index arrived and the screen never repainted — %r is still missing with nobody '
            'having clicked anything. chips=%r' % (want, late['chips']))
    assert not late['loading'], 'it still says "Loading your folders…" after the drive arrived'


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='no Chrome')
def test_a_warm_drive_is_never_blanked_while_it_refreshes():
    """The guard is on the DEFAULT. Guarding on `_pullDone` alone blanks a drive we can see."""
    early, late = asyncio.run(run(WARM_DRIVE))
    print('early=%r late=%r' % (early, late))
    for want in ('Backgrounds', 'Memes'):
        assert want in early['chips'], (
            'a client that already held real folders was shown "Loading…" instead of them — that '
            'is the same blank screen, arrived at from the other side. chips=%r loading=%r'
            % (early['chips'], early['loading']))
    assert not early['loading']


if __name__ == '__main__':
    pytest.main([__file__])
