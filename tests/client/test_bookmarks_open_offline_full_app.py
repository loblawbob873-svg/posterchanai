"""OFFLINE, BOOKMARKS SHOWS WHAT YOU SAVED — NOT "No bookmarks yet".

"Users should be able to access their notes, music, meme builder, news, bookmarks, messages, texts, etc
without network" (2026-10-09). BOOKMARKS was filled only once a relay was ready, so with no network the
view claimed you had none while your list and the posts sat in this device's Store.

Two phases in ONE browser profile: online, the real client loads the bookmark list and the post; then a
reload with every relay socket dead, and Bookmarks must still show the post.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


DEAD = r'''(()=>{let built=null;Object.defineProperty(window,'__events',{configurable:true,set(v){built=v;},get(){
  if(built||!window.NostrTools)return built||[];
  const me=new Uint8Array(32).fill(1), other=new Uint8Array(32).fill(7);
  const note=NostrTools.finalizeEvent({kind:1,created_at:1790000000,content:'my saved recipe for offline soup',tags:[]},other);
  const list=NostrTools.finalizeEvent({kind:10003,created_at:1790000100,content:'',tags:[['e',note.id]]},me);
  return built=[note,list];}});})();
if(localStorage.getItem('__dead')==='1'){
  window.WebSocket=class extends EventTarget{static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
    constructor(u){super();this.url=String(u);this.readyState=0;} send(){} close(){this.readyState=3;}};
  localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
}else{
  localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
}'''

SEED = r'''(()=>{const me=new Uint8Array(32).fill(1), other=new Uint8Array(32).fill(7);
  const note=NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000)-60,content:'my saved recipe for offline soup',tags:[]},other);
  const list=NostrTools.finalizeEvent({kind:10003,created_at:Math.floor(Date.now()/1000)-30,content:'',tags:[['e',note.id]]},me);
  window.__events=[note,list]; return note.id;})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_bookmarks_show_the_saved_post_with_no_network():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('bookmarks')")
        await b.until("document.querySelector('#feed') && /offline soup/.test(document.querySelector('#feed').innerText)")
        await asyncio.sleep(2)                       # the Store writes through to IndexedDB
        await b.js("localStorage.setItem('__dead','1')")
        await b.call('Page.reload')
        await b.until('!!window.__PC && !!window.PCOS')
        await b.until("!!__PC.me()")
        await b.js("__PC.switchView('bookmarks')")
        for _ in range(60):
            txt = await b.js("(document.querySelector('#feed')||{}).innerText||''")
            if 'offline soup' in txt:
                break
            await asyncio.sleep(.1)
        got['offline'] = await b.js("(document.querySelector('#feed')||{}).innerText||''")

    asyncio.run(desktop.with_browser('online', '', check, DEAD))
    assert 'offline soup' in got['offline'], ('Bookmarks lost the saved post offline', got['offline'][:300])
