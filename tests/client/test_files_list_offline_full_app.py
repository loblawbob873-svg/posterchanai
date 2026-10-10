"""OFFLINE, FILES STILL LISTS WHAT IS ON YOUR DRIVE.

"every app usable with no network" -- Files was the one left. The folder tree and every file's name live in the
encrypted drive index, which is kept on the device; but WHICH blobs exist comes from the media server's /list, and
that answer was only ever held in memory. So after a reload with no network, a folder you had just been looking
at said "Couldn't load files from ... (Failed to fetch)" over an empty grid -- the names were on the device, the
list that pairs them with blobs was not.

Two phases in ONE browser profile: online, the real client opens a folder and sees the file; then a reload with
no network at all (navigator.onLine false, the media server unreachable, every relay socket dead) -- the same
folder must still show the file, and say the list is the copy kept on this device.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

SHA = 'ab' * 32


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


INIT = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__listAsked=0;
const dead=localStorage.getItem('__dead')==='1';
if(dead){
  Object.defineProperty(Navigator.prototype,'onLine',{configurable:true,get:()=>false});
  window.WebSocket=class extends EventTarget{static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
    constructor(u){super();this.url=String(u);this.readyState=0;} send(){} close(){this.readyState=3;}};
}
const realFetch=window.fetch;
window.fetch=(url,opts)=>{const u=new URL(String(url),location.href);
  if(!/\/list\/[0-9a-f]{64}$/.test(u.pathname))return realFetch(url,opts);
  __listAsked++;
  if(dead)return Promise.reject(new TypeError('Failed to fetch'));
  return Promise.resolve(new Response(JSON.stringify([{sha256:'%(sha)s',size:4096,type:'application/pdf',uploaded:1760000000}]),
    {status:200,headers:{'Content-Type':'application/json'}}));};
''' % {'sha': SHA}

SEED = """(() => { const F = __PC.filesIdx();
  F.data = { folders: ['Music', 'Receipts'], files: { '%s': { name: 'tax-2026.pdf', folder: 'Receipts', type: 'application/pdf' } }, encFolders: [] };
  F._norm(); F._pullDone = true; F._pullOk = true; F._pullBlocked = false; F.saveLocal(); return true; })()""" % SHA

SHOWN = "!!document.querySelector('.file-card[data-sha=\"%s\"]')" % SHA


async def _open_folder(b):
    await b.js("__PC.switchView('blossom')")
    # the visible one: the sidebar and the home grid both carry the folder
    vis = "[...document.querySelectorAll('[data-folder=\"Receipts\"]')].find(e=>e.getClientRects().length&&e.offsetParent)"
    await b.until("!!" + vis)
    await b.js(vis + ".click()")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_a_folder_lists_its_files_with_no_network(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js(SEED)
        await _open_folder(b)
        await b.until(SHOWN)
        await asyncio.sleep(1.0)                        # the device copy is written behind the paint
        got['online_asked'] = await b.js('__listAsked')

        await b.js("localStorage.setItem('__dead','1')")
        await b.call('Page.reload')
        await b.until('!!window.__PC')
        await b.until('!!__PC.me()')
        await _open_folder(b)
        try:
            await b.until(SHOWN)
        except Exception:
            pass
        got['shown'] = await b.js(SHOWN)
        got['grid'] = await b.js("(document.querySelector('#bl-grid')||{}).textContent||''")
        got['pane'] = await b.js("document.body.innerText.slice(0,1500)")
        got['offline_asked'] = await b.js('__listAsked')

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    assert got['online_asked'] >= 1, got
    assert got['shown'], ('the folder lost its file offline', got['grid'][:300])
    assert 'kept on this device' in got['pane'], ('offline, and nothing says so', got['pane'][:600])
