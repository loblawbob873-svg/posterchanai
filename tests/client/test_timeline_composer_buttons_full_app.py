"""The composer at the top of the timeline, pressed in the SHIPPED bundle:

  * 📊 opens the poll builder, "Add option" adds a third option, Post signs a kind-1068 with all three;
  * 😀 → Emoji puts the picked emoji into the text;
  * ⋯ → Schedule → "Schedule" sends the signed note, with a FUTURE created_at, to /client/scheduled;
  * the media-grid tab switches the feed to the grid and back.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__sentEvents=[];window.__scheduled=[];
const __cSend=WebSocket.prototype.send;
WebSocket.prototype.send=function(raw){try{const m=JSON.parse(raw);if(m[0]==='EVENT'&&m[1])__sentEvents.push(m[1]);}catch(_){}return __cSend.call(this,raw)};
const __tf=window.fetch;
window.fetch=(u,o)=>{const s=String(u);
  if(s.includes('/client/scheduled/list'))return Promise.resolve(new Response(JSON.stringify({ok:true,items:[]}),{status:200,headers:{'Content-Type':'application/json'}}));
  if(s.includes('/client/scheduled')){__scheduled.push(JSON.parse(o.body));return Promise.resolve(new Response(JSON.stringify({ok:true}),{status:200,headers:{'Content-Type':'application/json'}}));}
  return __tf(u,o);};
'''


async def home(b):
    await desktop.login(b)
    await b.js("window.__publishOK=true; __PC.switchView('home'); 1")
    await b.until("!!document.querySelector('#tl-cmp-ta') && !!document.querySelector('#tl-cmp-poll')")


async def type_text(b, v):
    await b.js("(()=>{const t=document.querySelector('#tl-cmp-ta');t.focus();t.value=%r;t.dispatchEvent(new Event('input',{bubbles:true}));})(); 1" % v)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_inline_poll_emoji_and_schedule():
    async def check(b):
        await home(b)
        await type_text(b, "Tabs or spaces?")
        await b.js("document.querySelector('#tl-cmp-poll').click(); 1")
        await b.until("!document.querySelector('#tl-cmp-pollbox').classList.contains('hidden')")
        await b.js("document.querySelector('#tl-cmp-poll-add').click(); 1")
        await b.until("document.querySelectorAll('#tl-cmp-poll-opts .poll-opt-in').length===3")
        await b.js("(()=>{const o=document.querySelectorAll('#tl-cmp-poll-opts .poll-opt-in');o[0].value='Tabs';o[1].value='Spaces';o[2].value='Both';})(); 1")
        await b.js("document.querySelector('#tl-cmp-post').click(); 1")
        await b.until("__sentEvents.some(e=>e.kind===1068)")
        poll = await b.js("__sentEvents.find(e=>e.kind===1068)")
        assert [t[2] for t in poll["tags"] if t[0] == "option"] == ["Tabs", "Spaces", "Both"], poll["tags"]

        # Emoji.
        await b.until("document.querySelector('#tl-cmp-ta').value===''")
        await b.js("document.querySelector('#tl-cmp-react').click(); 1")
        await b.until("!!document.querySelector('[data-m=emoji]')")
        await b.js("document.querySelector('[data-m=emoji]').click(); 1")
        await b.until("!!document.querySelector('.emoji-pop .ep-grid button[data-e]')")
        emoji = await b.js("(()=>{const e=document.querySelector('.emoji-pop .ep-grid button[data-e]');"
                           "e.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}));return e.dataset.e})()")
        await b.until("document.querySelector('#tl-cmp-ta').value.includes(%r)" % emoji)

        # Schedule.
        await type_text(b, "posted later")
        await b.js("document.querySelector('#tl-cmp-more').click(); 1")
        await b.until("!!document.querySelector('[data-m=sched]')")
        await b.js("document.querySelector('[data-m=sched]').click(); 1")
        await b.until("!document.querySelector('#tl-cmp-schedrow').classList.contains('hidden')")
        await b.js("document.querySelector('#tl-cmp-sched-go').click(); 1")
        await b.until("__scheduled.length===1")
        ev = await b.js("__scheduled[0].event || __scheduled[0]")
        assert ev["content"] == "posted later" and ev["created_at"] > await b.js("Math.floor(Date.now()/1000)+60"), ev
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_media_grid_tab_toggles():
    async def check(b):
        await home(b)
        await b.until("!!document.querySelector('#tl-media-tab')")
        await b.js("document.querySelector('#tl-media-tab').click(); 1")
        await b.until("!!document.querySelector('#tl-media-tab.on')")
        await b.js("document.querySelector('#tl-media-tab').click(); 1")
        await b.until("!!document.querySelector('#tl-media-tab') && !document.querySelector('#tl-media-tab.on')")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
