"""A live stream's page, pressed in the SHIPPED bundle on someone else's live stream (a kind-30311):

  * Send posts a kind-1311 chat line addressed to the stream (`a` = 30311:<host>:<d>);
  * on a phone, Chat hides the chat (and stops reading it) and shows it again;
  * on a desktop, 🗔 Window opens the stream in a separate window, and Pop out moves the player into the corner
    mini-player, whose ⤢ brings you back to the stream and ✕ closes it;
  * Tip opens the tip sheet for the HOST, and ← Streams goes back to the list.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__sent=[];window.__opened=[];
const __ss=WebSocket.prototype.send;
WebSocket.prototype.send=function(r){try{const m=JSON.parse(r);if(m[0]==='EVENT')__sent.push(m[1]);}catch(_){}return __ss.call(this,r)};
window.open=(u,n)=>{__opened.push(String(u||''));return {closed:false,focus(){},close(){}};};
'''

STREAM = r'''(()=>{const sk=new Uint8Array(32).fill(3),now=Math.floor(Date.now()/1000);
  const prof=NostrTools.finalizeEvent({kind:0,created_at:now-99,tags:[],content:JSON.stringify({name:'Host',lud16:'host@ln.example'})},sk);
  const ev=NostrTools.finalizeEvent({kind:30311,created_at:now-30,content:'',tags:[['d','show-1'],['title','Evening show'],['status','live'],
    ['streaming','https://fixture.invalid/hls/show-1/index.m3u8'],['starts',String(now-600)]]},sk);
  Store.saveProfile(prof);Store.saveEvent(ev);window.__stream=ev;window.__host=NostrTools.getPublicKey(sk);
  __PC.openStream(ev);return 1;})()'''


async def open_stream(b, width):
    await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
    await desktop.login(b)
    await b.js("window.__publishOK=true; 1")
    await b.js(STREAM)
    await b.until("!!document.querySelector('#st-back') && !!document.querySelector('#st-chat-send')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_chat_and_chat_toggle_on_a_phone_tip_and_back():
    async def check(b):
        await open_stream(b, 390)
        await b.js("(()=>{const i=document.querySelector('#st-chat-inp');i.value='hello stream';document.querySelector('#st-chat-send').click();})(); 1")
        await b.until("__sent.some(e=>e.kind===1311&&e.content==='hello stream')")
        line = await b.js("__sent.find(e=>e.kind===1311)")
        assert ["a", f"30311:{await b.js('__host')}:show-1"] == line["tags"][0][:2], line["tags"]
        await b.until("/hello stream/.test(document.querySelector('#st-chat-msgs').textContent)")

        await b.js("document.querySelector('#st-chat-toggle').click(); 1")
        await b.until("document.querySelector('.stream-layout').classList.contains('chat-hidden')")
        await b.js("document.querySelector('#st-chat-toggle').click(); 1")
        await b.until("!document.querySelector('.stream-layout').classList.contains('chat-hidden')")

        await b.js("document.querySelector('#st-tip').click(); 1")
        await b.until("!!document.querySelector('#zap-go')")          # the host advertises only Lightning
        await b.js("__PC.closeModal(); 1")
        await b.js("document.querySelector('#st-back').click(); 1")
        await b.until("!document.querySelector('#st-back')")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_window_and_pop_out_on_a_desktop():
    async def check(b):
        await open_stream(b, 1366)
        await b.until("!!document.querySelector('#st-window') && !!document.querySelector('#st-pop')")
        await b.js("document.querySelector('#st-window').click(); 1")
        await b.until("__opened.some(u=>/naddr1/.test(u))")
        await b.js("document.querySelector('#st-pop').click(); 1")
        await b.until("!!document.querySelector('#mini-player.on #mini-open')")
        await b.js("document.querySelector('#mini-open').click(); 1")
        await b.until("!!document.querySelector('#st-back') && !document.querySelector('#mini-player.on')")
        await b.js("document.querySelector('#st-pop').click(); 1")
        await b.until("!!document.querySelector('#mini-player.on #mini-close')")
        await b.js("document.querySelector('#mini-close').click(); 1")
        await b.until("!document.querySelector('#mini-player.on')")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
