"""News: adding, removing, picking, importing and exporting feeds all work through the real UI (2026-10-22: every
screen and button gets a test -- News had none of its 7 buttons named by any test).

The screen reads feeds through the node's /api/rss/feed(s) (fixtures here) and keeps the person's feed list in
localStorage + a kind-30078 doc. A person must be able to add a feed and see its stories, be told a duplicate is a
duplicate, pick one feed, remove it, import feeds from pasted OPML, and export what they have as OPML. The AI
summary button is out of scope ("not AI at this time"). Real login, DOM and news.js.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

FEATURE_COVERS = ("news",)

FEED = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__sentEvents=[];
const __nSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){try{const m=JSON.parse(raw);if(m[0]==='EVENT'&&m[1])__sentEvents.push(m[1]);}catch(_){}return __nSend.call(this,raw)};
const __nFetch=window.fetch;
window.__rss={asked:[]};
const __story=(u,i)=>({title:'Story '+i+' from '+u.split('/')[2],link:u+'/s'+i,ts:Math.floor(Date.now()/1000)-i*60,summary:'s'});
window.fetch=function(url,opts){
  const u=String(url);
  if(u.includes('/api/rss/feeds?')){ __rss.asked.push(u); const urls=decodeURIComponent(u.split('urls=')[1]).split(',');
    return Promise.resolve(new Response(JSON.stringify({feeds:urls.map(x=>({url:x,items:[__story(x,1),__story(x,2)]}))}),{status:200,headers:{'Content-Type':'application/json'}})); }
  if(u.includes('/api/rss/feed?')){ __rss.asked.push(u); const x=decodeURIComponent(u.split('url=')[1]);
    return Promise.resolve(new Response(JSON.stringify({items:[__story(x,1)]}),{status:200,headers:{'Content-Type':'application/json'}})); }
  return __nFetch(url,opts);
};
window.__downloads=[];
const __click=HTMLAnchorElement.prototype.click;
HTMLAnchorElement.prototype.click=function(){ if(this.download){ __downloads.push({name:this.download,href:this.href}); return; } return __click.call(this); };
'''

OPML = ('<opml version="2.0"><body><outline text="Two" xmlUrl="https://two.example/rss"/>'
        '<outline text="Three" xmlUrl="https://three.example/rss"/></body></opml>')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _set(b, sel, value):
    import json
    await b.js(f"(()=>{{const e=document.querySelector({json.dumps(sel)});e.value={json.dumps(value)};e.dispatchEvent(new Event('input',{{bubbles:true}}));}})()")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_feeds_add_pick_remove_import_and_export():
    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('news'); 1")
        await b.until("!!document.querySelector('#news-pick') && !!document.querySelector('.news-add')")
        # Add a feed (the ＋ chip): it is saved (localStorage + the account doc) and its stories appear.
        await b.js("document.querySelector('.news-add').click(); 1")
        await b.until("!!document.querySelector('#news-newurl')")
        await _set(b, "#news-newurl", "one.example/rss")                 # no scheme: https:// is added
        await _set(b, "#news-newname", "One")
        await b.js("document.querySelector('#news-addbtn').click(); 1")
        await b.until("[...document.querySelectorAll('.news-title')].some(a=>/Story 1 from one.example/.test(a.textContent))")
        assert await b.js("__sentEvents.some(e=>e.kind===30078&&(e.tags||[]).some(t=>t[0]==='d'&&t[1]==='pcai:news-feeds'))"), \
            "the feed list was not saved to the account"
        # A duplicate is refused, not added twice.
        await b.js("document.querySelector('.news-add').click(); 1")
        await b.until("!!document.querySelector('#news-newurl')")
        await _set(b, "#news-newurl", "https://one.example/rss")
        await b.js("document.querySelector('#news-addbtn').click(); 1")
        await b.until("/already added/.test(document.body.innerText)")
        # Import two more from pasted OPML.
        await _set(b, "#news-opml-text", OPML)
        await b.js("document.querySelector('#news-import-text').click(); 1")
        await b.until("/imported 2 feeds/.test(document.body.innerText)")
        # Pick one feed: only its stories, asked of the single-feed endpoint.
        await b.until("!!document.querySelector('#news-pick')")
        await b.js("document.querySelector('#news-pick').click(); 1")
        await b.until("!!document.querySelector('[data-feed=\"https://three.example/rss\"]')")
        await b.js("document.querySelector('[data-feed=\"https://three.example/rss\"]').click(); 1")
        await b.until("__rss.asked.some(u=>u.includes('/api/rss/feed?url=https%3A%2F%2Fthree.example'))")
        await b.until("[...document.querySelectorAll('.news-title')].every(a=>/three.example/.test(a.textContent))")
        # Export (reached through the picker's own "Manage feeds"): an OPML file is handed to the browser.
        await b.js("document.querySelector('#news-pick').click(); 1")
        await b.until("!!document.querySelector('#news-fmanage')")
        await b.js("document.querySelector('#news-fmanage').click(); 1")
        await b.until("!!document.querySelector('#news-export')")
        await b.js("document.querySelector('#news-export').click(); 1")
        await b.until("__downloads.some(d=>d.name==='posterchan-feeds.opml')")
        # Remove one: it leaves the list.
        await b.js("document.querySelector('.news-del').click(); 1")
        await b.until("document.querySelectorAll('.news-mrow').length===2")   # the dialog redraws with the feed gone
        names = await b.js("[...document.querySelectorAll('.news-mrow span')].map(s=>s.textContent)")
        assert "One" not in names and len(names) == 2, names
    asyncio.run(desktop.with_browser('online', '', check, FEED))
