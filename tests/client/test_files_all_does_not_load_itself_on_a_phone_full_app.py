"""Files -> All on a phone shows one page and loads the next only when you scroll to it.

"files -> all on android is taking forever to load causing me not able to open video files and play them or
view images ... 5225 files" (2026-10-16). Measured on that drive at phone size: the grid loaded page after page
with nobody scrolling -- 60 -> 2,040 cards in 90 s, every step re-rendering the whole grid -- because the
auto-load observer's root was `.fx-main`, which on a phone does not scroll (the `#files-pane` around it does), so
the "Load more" button was always inside its root. Drives the shipped app at phone size with a 600-file drive.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


HOST = r'''
const _nf=window.fetch;
window.fetch=async function(url,opts={}){const u=String(url);
 if(/\/list\/[0-9a-f]{64}/.test(u)){
   const out=[];for(let i=0;i<600;i++){const h=(i.toString(16).padStart(4,'0')).repeat(16);
     out.push({sha256:h,size:1234+i,type:'application/pdf',uploaded:1700000000+i,url:location.origin+'/'+h+'.pdf'});}
   return new Response(JSON.stringify(out),{status:200,headers:{'Content-Type':'application/json'}});}
 return _nf(url,opts);};
window.pcShell.windowContext={role:'app',view:'blossom'};window.pcShell.backgroundOwner=false;
'''

CARDS = "document.querySelectorAll('.file-card[data-sha]').length"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_all_files_on_a_phone_waits_for_the_scroll():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 393, 'height': 851, 'deviceScaleFactor': 2.75, 'mobile': True})
        await desktop.login(b)
        await b.until("!!document.querySelector('.folder-chip[data-folder=\"\"]')")
        await b.js("document.querySelector('.folder-chip[data-folder=\"\"]').click()")
        await b.until(CARDS + " >= 60")
        await asyncio.sleep(4)                                   # nobody scrolls
        got['idle'] = await b.js(CARDS)
        got['pane_scrolls'] = await b.js("(()=>{const p=document.getElementById('files-pane');return !!p&&p.scrollHeight>p.clientHeight})()")
        await b.js("(()=>{const p=document.getElementById('files-pane');p.scrollTop=p.scrollHeight})()")
        await b.until(CARDS + " >= 120")
        got['after_scroll'] = await b.js(CARDS)

    asyncio.run(desktop.with_browser('online', '?pcwin=blossom', check, HOST))
    assert got['pane_scrolls'], ('the phone layout no longer scrolls #files-pane: re-check this test', got)
    assert got['idle'] == 60, ('the grid loaded more pages with nobody scrolling', got)
    assert got['after_scroll'] == 120, ('scrolling to the end must load the next page', got)
