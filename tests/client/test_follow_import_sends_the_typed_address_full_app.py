"""Settings → Fediverse → import: the address typed in the box must reach the server.

Reported as 'it says I need to connect an account first' by somebody who had typed one: the
server only says that when the request carries no address. Drives the real bundled client in a
real desktop window, types into the box the person can SEE, clicks, and reads the request."""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_typed_address_is_what_the_import_sends():
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('settings')")
        await b.until("!!document.querySelector('#us-plr-import')")
        await b.js("""
          document.querySelector('.us-tab[data-tab=social]')?.click();
          window.__importBodies=[];
          const originalFetch=window.fetch;
          window.fetch=function(url,opts={}){
            if(String(url).includes('/api/activitypub/import-following')){
              __importBodies.push(opts.body||'');
              return Promise.resolve(new Response(JSON.stringify({following:0,people:[]}),
                                     {status:200,headers:{'Content-Type':'application/json'}}));
            }
            return originalFetch.apply(this,arguments);
          };
        """)
        # Type into the box that is actually on screen -- whatever copies exist in the document.
        typed = await b.js("""(()=>{
          const boxes=[...document.querySelectorAll('#us-plr-import-acct')];
          const seen=boxes.find(x=>x.offsetParent!==null) || boxes[boxes.length-1];
          seen.value='me@old.example'; seen.dispatchEvent(new Event('input',{bubbles:true}));
          seen.closest('.us-plr-import').querySelector('#us-plr-import').click();
          return boxes.length;
        })()""")
        assert typed >= 1
        await b.until('__importBodies.length>0')
        body = await b.js('__importBodies[0]')
        assert 'me@old.example' in body, f'the import sent {body!r} with {typed} box(es) on the page'

    asyncio.run(desktop.with_browser('online', '', check, ''))
