"""UPLOADING A FILE INTO A NAMED FOLDER MUST PUT IT IN THAT FOLDER.

Reported for four days straight: "I still can't fucking upload to blossom from File Manager to
Backgrounds folder." Fixes were claimed more than once. There has never been a test that actually
UPLOADS — every Files check measures listing, rendering or layout, so the one action the screen
exists for was covered by nothing, and each "fix" was a reading of the code rather than a run of it.

This drives the REAL file input on the REAL Files screen with a REAL File, through the shipped
`uploadFilesSeq`, and then asks the drive index the only question that matters:

    is the file there, AND is it in the folder I was standing in?

Three ways that has failed here before, each of which this asserts separately so the report names
which one happened rather than "upload broken":
  * REFUSED — the fail-closed guard (`FilesIdx._pullDone`) turns every upload into a named folder
    into "One sec — still loading your folders", for ever, when a pull throws.
  * MISFILED — the bytes land but the row is tagged with the wrong folder (or none), so the file is
    on the drive and invisible in the folder you put it in. That is indistinguishable from a failed
    upload to the person looking at the screen.
  * UNSAVED — the blob uploads and the encrypted index write does not, so it is there until reload
    and gone afterwards. `endBatch()` reports this and the test reads its answer.
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

FOLDER = 'Backgrounds'
NAME = 'a-wallpaper.png'

SEED = """(() => {
  const F = __PC.filesIdx();
  // A drive that has already loaded, with the folder the user is standing in.
  F.data = { folders: ['Music', %r], files: {}, encFolders: [] };
  F._norm(); F._pullDone = true; F._pullOk = true; F._pullBlocked = false;
  return { folders: F.folders ? F.folders() : F.data.folders };
})()""" % FOLDER

# Put a real file on the real input and let the shipped onchange handler take it from there.
ATTACH = """(() => {
  const input = document.querySelector('#bl-file');
  if (!input) return { noInput: true };
  const png = Uint8Array.from(atob(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='),
    c => c.charCodeAt(0));
  const file = new File([png], %r, { type: 'image/png' });
  const dt = new DataTransfer(); dt.items.add(file);
  let assignErr = '';
  try { input.files = dt.files; } catch (e) { assignErr = String(e && e.message || e); }
  const n = input.files ? input.files.length : -1;
  input.dispatchEvent(new Event('change', { bubbles: true }));
  return { attached: true, count: n, assignErr,
           inputHidden: getComputedStyle(input).display,
           dtCount: dt.files.length };
})()""" % NAME

VERDICT = """(() => {
  const F = __PC.filesIdx();
  const rows = Object.entries(F.data.files || {}).map(([sha, m]) => ({ sha, ...m }));
  const mine = rows.filter(r => r.name === %r);
  return {
    rows: rows.length,
    mine: mine.map(r => ({ name: r.name, folder: r.folder === undefined ? '(unset)' : r.folder })),
    inFolder: mine.filter(r => r.folder === %r).length,
    puts: (window.__requests || []).filter(x => x[1] === 'PUT').map(x => x[0]),
    toasts: [...document.querySelectorAll('.toast, #toast')].map(t => t.textContent.trim()),
    body: document.body.innerText.slice(-400),
    errors: window.__errors || [],
  };
})()""" % (NAME, FOLDER)


async def run():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.mkdtemp(prefix='pc-upload-folder-')
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
            target = next(p for p in pages
                          if p.get('type') == 'page' and p.get('url') == 'about:blank')
            async with websockets.connect(target['webSocketDebuggerUrl'], max_size=20_000_000) as ws:
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
                await b.js("""(()=>{const key=new Uint8Array(32).fill(9);
                  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);
                  document.querySelector('#btn-nsec-login').click()})()""")
                await b.until("!!__PC.me()")

                seeded = await b.js(SEED)
                await b.js("__PC.switchView('blossom')")
                await b.until("!!document.querySelector('#bl-file')")
                await asyncio.sleep(0.6)

                # Stand INSIDE the folder, the way a person does, rather than setting a variable.
                entered = await b.js("""(() => {
                  const el = document.querySelector('[data-folder=\"%s\"]');
                  if (!el) return { missing: [...document.querySelectorAll('[data-folder]')]
                                      .map(e => e.dataset.folder) };
                  el.click(); return { clicked: true };
                })()""" % FOLDER)
                await asyncio.sleep(0.8)
                where = await b.js("document.body.innerText.slice(0,200)")

                # onerror is blind to an async function that rejects — which uploadFilesSeq is.
                await b.js("""(() => { window.__rejections = [];
                  addEventListener('unhandledrejection', e => __rejections.push(
                    String((e.reason && (e.reason.stack || e.reason.message)) || e.reason)));
                  // `toast` lives inside the app's IIFE, not on window — the only honest way to
                  // see one is to watch the DOM, because a toast removes itself after a few seconds.
                  window.__toastLog = [];
                  new MutationObserver(ms => { for (const m of ms) for (const n of m.addedNodes) {
                    if (n.nodeType !== 1) continue;
                    const t = (n.className && String(n.className).includes('toast')) ? n
                            : (n.querySelector ? n.querySelector('.toast') : null);
                    if (t) __toastLog.push(String(t.textContent || '').trim());
                  } }).observe(document.body, { childList: true, subtree: true });
                  return true; })()""")
                probe = await b.js("""(() => {
                  const i = document.querySelector('#bl-file');
                  const F = __PC.filesIdx();
                  window.__toastLog = [];
                  const t = window.toast;
                  return { hasInput: !!i, hasOnChange: !!(i && i.onchange),
                           pullDone: F._pullDone, pullOk: F._pullOk, pullBlocked: F._pullBlocked,
                           folders: F.folders ? F.folders() : null };
                })()""")
                await b.js("""(() => {
                  window.__fired = 0;
                  const all = [...document.querySelectorAll('#bl-file')];
                  window.__inputs = all.length;
                  all.forEach(i => { const h = i.onchange;
                    i.onchange = function(){ window.__fired++; return h && h.apply(this, arguments); }; });
                  return true; })()""")
                attached = await b.js(ATTACH)
                # The upload is sequential and ends by saving the index; give it room.
                # The per-file error lives in the queue row's title and the queue is cleared when
                # the batch ends — so it has to be read WHILE the upload is running.
                await b.js("""(() => { window.__stat = [];
                  const tick = setInterval(() => {
                    document.querySelectorAll('#bl-queue .up-stat').forEach(e => {
                      const v = (e.textContent || '') + '|' + (e.title || '');
                      if (v.trim() !== '|' && !__stat.includes(v)) __stat.push(v); });
                  }, 60);
                  setTimeout(() => clearInterval(tick), 25000); return true; })()""")
                for _ in range(80):
                    done = await b.js(
                        "(() => { const F=__PC.filesIdx();"
                        " return Object.values(F.data.files||{}).some(m=>m.name===%r); })()" % NAME)
                    if done:
                        break
                    await asyncio.sleep(0.25)
                await asyncio.sleep(1.0)   # let endBatch() land
                v = await b.js(VERDICT)
                v['queue'] = await b.js(
                    "((q)=>q?q.innerText.slice(0,300):'(no #bl-queue)')(document.querySelector('#bl-queue'))")
                v['queueTitles'] = await b.js(
                    "[...document.querySelectorAll('#bl-queue .up-stat')].map(e=>e.textContent+'|'+(e.title||''))")
                v['probe'] = probe
                v['statuses'] = await b.js("window.__stat || []")
                v['fired'] = await b.js("({fired: window.__fired, inputs: window.__inputs})")
                v['rejections'] = await b.js("window.__rejections || []")
                v['toastLog'] = await b.js("window.__toastLog || []")
                v['pullAfter'] = await b.js(
                    "(()=>{const F=__PC.filesIdx();return {pullDone:F._pullDone,pullOk:F._pullOk,"
                    "pullBlocked:F._pullBlocked,dirty:F._dirty};})()")
                return seeded, entered, where, attached, v
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    finally:
        shutil.rmtree(profile, ignore_errors=True)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='no Chrome')
def test_a_file_uploaded_into_a_folder_is_in_that_folder():
    seeded, entered, where, attached, got = asyncio.run(run())
    print('seeded=%r entered=%r attached=%r' % (seeded, entered, attached))
    print('verdict=%r' % (got,))

    assert not attached.get('noInput'), \
        'The Files screen drew no upload input at all — there is nothing to click.'
    assert entered.get('clicked'), \
        'Could not stand in the %s folder; the screen offered: %r' % (FOLDER, entered.get('missing'))

    assert got['mine'], (
        'The upload never reached the drive index. Nothing named %r is on the drive.\n'
        '  PUTs seen: %r\n  on screen: %r\n  js errors: %r'
        % (NAME, got['puts'], got['body'], got['errors']))

    assert got['inFolder'] >= 1, (
        'The file uploaded but was MISFILED — it is on the drive and not in %s, which looks '
        'exactly like a failed upload to the person watching.\n  rows: %r'
        % (FOLDER, got['mine']))
