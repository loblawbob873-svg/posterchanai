"""Opening Files downloads the drive listing ONCE, and an upload still shows at once.

"files -> all on android is taking forever to load ... 5225 files" (2026-10-16). Every repaint of Files fetched the
whole `/list/<pubkey>` again (~2 MB on that drive), and one open repaints by itself up to three times -- the drive
index arriving, the synced folders arriving, the drive list changing -- so a phone downloaded the listing three times
before it showed anything. Here the listing is slow (800 ms) and two repaints land while it is in flight: one
download. Then a file is uploaded and Files repaints: the new file is there (the remembered listing is stale the
moment a blob store is written to).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


HOST = r'''
window.__files=[];for(let i=0;i<600;i++){const h=(i.toString(16).padStart(4,'0')).repeat(16);
  window.__files.push({sha256:h,size:1234+i,type:'application/pdf',uploaded:1700000000+i,url:location.origin+'/'+h+'.pdf'});}
window.__lists=0;
const _nf=window.fetch;
window.fetch=async function(url,opts={}){const u=String(url),m=String((opts&&opts.method)||'GET').toUpperCase();
 if(/\/list\/[0-9a-f]{64}/.test(u)){ window.__lists++; await new Promise(r=>setTimeout(r,800));
   return new Response(JSON.stringify(window.__files),{status:200,headers:{'Content-Type':'application/json'}});}
 if(m==='PUT'&&/\/upload$/.test(u)){ const h='f00d'.repeat(16);
   window.__files=[{sha256:h,size:99,type:'application/pdf',uploaded:1800000000,url:location.origin+'/'+h+'.pdf'},...window.__files];
   return new Response(JSON.stringify({sha256:h,size:99,url:location.origin+'/'+h+'.pdf'}),{status:200,headers:{'Content-Type':'application/json'}});}
 return _nf(url,opts);};
// A repaint the app makes by itself: the account's synced-folder list arriving/changing (pc:sync-folders).
window.__driveTick=n=>{const S=window.PCSync=window.PCSync||{};const pairs=Array.from({length:n},(_,i)=>({key:'folder'+i,n:0}));
  S.acct=()=>pairs;if(!S.folders)S.folders=()=>[];window.dispatchEvent(new Event('pc:sync-folders'));};
window.pcShell.windowContext={role:'app',view:'blossom'};window.pcShell.backgroundOwner=false;
'''

CARDS = "document.querySelectorAll('.file-card[data-sha]').length"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_one_open_downloads_the_listing_once_and_an_upload_still_shows():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 412, 'height': 900, 'deviceScaleFactor': 2.75, 'mobile': True})
        await desktop.login(b)
        await b.until("!!document.querySelector('.folder-chip[data-folder=\"\"]')")
        base = await b.js("window.__lists")
        await b.js("document.querySelector('.folder-chip[data-folder=\"\"]').click()")
        await asyncio.sleep(0.2)
        await b.js("window.__driveTick(1)")                       # repaints while the listing is downloading
        await asyncio.sleep(0.2)
        await b.js("window.__driveTick(2)")
        await b.until(CARDS + " >= 60")
        await asyncio.sleep(2)
        got['open'] = await b.js("window.__lists") - base
        before = await b.js("window.__lists")
        await b.js("fetch((typeof mediaServer==='function'?mediaServer():(__PC.mediaServer?__PC.mediaServer():location.origin)).replace(/\\/$/,'')+'/upload',{method:'PUT',body:'x'})")
        await asyncio.sleep(0.3)
        await b.js("window.__driveTick(3)")                       # the next repaint after an upload
        await b.until("!!document.querySelector('.file-card[data-sha=\"" + 'f00d' * 16 + "\"]')")
        got['after_upload'] = await b.js("window.__lists") - before

    asyncio.run(desktop.with_browser('online', '?pcwin=blossom', check, HOST))
    assert got['open'] == 1, ('one open of Files downloaded the drive listing %d times' % got['open'], got)
    assert got['after_upload'] == 1, ('a repaint after an upload must fetch the listing again (once)', got)
