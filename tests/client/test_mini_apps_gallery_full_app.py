"""The Mini apps (webxdc) gallery lists what people posted, refreshes on request, and explains an empty state
(2026-10-22: every screen gets a browser test).

The gallery reads the local store, then asks relays for kind-1063 file events with the webxdc MIME type (and notes
tagged #webxdc). Fed one posted app through the real Relay.query seam, it must show that app by name; Refresh must
really ask again; with nothing posted it must say how to post one. Real login, DOM and webxdc.js.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FEED = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''

SETUP = r'''(()=>{
  window.__xdc={mode:'one',asks:0};
  const app={id:'a'.repeat(64),pubkey:'b'.repeat(64),kind:1063,created_at:Math.floor(Date.now()/1000)-60,content:'try my poll',sig:'s',
    tags:[['m','application/x-webxdc'],['url','https://cdn.example/pixel-poll.xdc'],['x','c'.repeat(64)],['alt','Webxdc app: Pixel Poll'],['webxdc','pixel-poll-1']]};
  const q=Relay.query.bind(Relay);
  Relay.query=async(filters,...rest)=>{
    if((filters||[]).some(f=>(f.kinds||[]).includes(1063)||(f['#t']||[]).includes('webxdc'))){
      __xdc.asks++; const out=__xdc.mode==='one'?[app]:[]; Object.defineProperty(out,'complete',{value:true}); return out; }
    return q(filters,...rest);
  };
  __PC.switchView('xdc'); return 1;
})()'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_gallery_lists_a_posted_app_refreshes_and_explains_empty():
    async def check(b):
        await desktop.login(b)
        await b.js(SETUP)
        await b.until("/Pixel Poll/.test((document.querySelector('.xdc-grid')||{}).innerText||'')")
        asked = await b.js("__xdc.asks")
        assert asked >= 1
        await b.until("!document.querySelector('#xdc-gal-refresh').disabled")
        await b.js("document.querySelector('#xdc-gal-refresh').click(); 1")
        await b.until(f"__xdc.asks>{asked}")                   # Refresh really asked the relays again
        await b.until("!document.querySelector('#xdc-gal-refresh').disabled")
        assert await b.js("/Pixel Poll/.test(document.querySelector('#feed').innerText)")
    asyncio.run(desktop.with_browser('online', '', check, FEED))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_with_nothing_posted_it_says_how_to_post_one():
    async def check(b):
        await desktop.login(b)
        await b.js(SETUP.replace("mode:'one'", "mode:'none'"))
        await b.until("/No mini apps found yet/.test(document.querySelector('#feed').innerText)")
        assert await b.js("/Mini app/.test(document.querySelector('#feed').innerText)")
    asyncio.run(desktop.with_browser('online', '', check, FEED))
