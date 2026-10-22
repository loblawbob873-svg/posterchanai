"""Go Live, pressed in the SHIPPED bundle against a stubbed ingest endpoint:

  * an account not allowed to stream gets "Request streaming access", which records the request with the
    server AND messages the operator; Close closes;
  * an account that may stream: Save as template → name → Save stores a template, Delete (confirmed) removes it;
    "Announce and Stream" (OBS) publishes the live event (kind 30311, status live), and End stream publishes it
    ended.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def fixture(ingest):
    return r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__sent=[];window.__posts=[];
const __gs=WebSocket.prototype.send;
WebSocket.prototype.send=function(r){try{const m=JSON.parse(r);if(m[0]==='EVENT')__sent.push(m[1]);}catch(_){}return __gs.call(this,r)};
const __gf=window.fetch;
window.fetch=(u,o)=>{const s=String(u),json=(v,st=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:st,headers:{'Content-Type':'application/json'}}));
  if(s.includes('/api/streams/ingest'))return json(INGEST);
  if(s.includes('/client/stream-request')){__posts.push(JSON.parse(o.body));return json({ok:true});}
  if(s.includes('/hls/')||s.includes('.m3u8'))return Promise.resolve(new Response('#EXTM3U\n',{status:200}));
  return __gf(u,o);};
'''.replace('INGEST', json.dumps(ingest))


async def go_live_sheet(b):
    await desktop.login(b)
    await b.js("window.__publishOK=true; __PC.CFG.operator_npub=NostrTools.nip19.npubEncode(NostrTools.getPublicKey(new Uint8Array(32).fill(4))); __PC.switchView('streams'); 1")
    await b.until("!!document.querySelector('#stream-golive')")
    await b.js("document.querySelector('#stream-golive').click(); 1")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_request_streaming_access_and_close():
    async def check(b):
        await go_live_sheet(b)
        await b.until("!!document.querySelector('#sreq')")
        await b.js("document.querySelector('#sreq').click(); 1")
        await b.until("__posts.length===1 && /Request sent/.test(document.querySelector('#sreq').textContent)")
        assert await b.js("__posts[0].pubkey===__PC.me().pubkey")
        assert await b.js("__sent.some(e=>e.kind===1059)"), "the operator was not messaged"
        await b.js("document.querySelector('#sreqx').click(); 1")
        await b.until("!document.querySelector('#sreqx')")
    asyncio.run(desktop.with_browser('online', '', check,
                                     fixture({"error": "no_permission", "message": "Live streaming isn't on for you yet."})))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_template_save_and_delete_then_go_live_from_obs_and_end():
    ingest = {"enabled": True, "rtmp_url": "rtmp://fixture.invalid/live", "stream_key": "key123", "token": "tok123",
              "hls_url": "https://fixture.invalid/hls/tok123/index.m3u8"}

    async def check(b):
        await go_live_sheet(b)
        await b.until("!!document.querySelector('#gl-go')")
        await b.js("document.querySelector('#gl-title').value='Friday stream'; 1")
        await b.js("document.querySelector('#gl-tpl-save').click(); 1")
        await b.until("!document.querySelector('#gl-tpl-namerow').classList.contains('hidden')")
        await b.js("document.querySelector('#gl-tpl-name').value='Fridays'; document.querySelector('#gl-tpl-ok').click(); 1")
        await b.until("[...document.querySelector('#gl-tpl-sel').options].some(o=>o.value==='Fridays'||o.textContent==='Fridays')")
        await b.until("!document.querySelector('#gl-tpl-del').classList.contains('hidden')")
        await b.js("document.querySelector('#gl-tpl-del').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        await b.until("![...document.querySelector('#gl-tpl-sel').options].some(o=>o.textContent==='Fridays')")

        await b.js("(()=>{const r=document.querySelector('input[name=gl-src][value=obs]');if(r){r.checked=true;r.dispatchEvent(new Event('change',{bubbles:true}));}"
                   "const a=document.querySelector('#gl-announce');if(a){a.checked=false;a.dispatchEvent(new Event('change',{bubbles:true}));}})(); 1")
        await b.until("!document.querySelector('#gl-go').disabled")
        await b.js("document.querySelector('#gl-go').click(); 1")
        live = "__sent.find(e=>e.kind===30311&&e.tags.some(t=>t[0]==='status'&&t[1]==='live'))"
        await b.until(f"!!{live}")
        assert await b.js(f"{live}.tags.find(t=>t[0]==='title')[1]") == "Friday stream"
        await b.js("__PC.switchView('streams'); 1")
        await b.until("!!document.querySelector('#stream-end')")
        await b.js("document.querySelector('#stream-end').click(); 1")
        await b.until("__sent.some(e=>e.kind===30311&&e.tags.some(t=>t[0]==='status'&&t[1]==='ended'))")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, fixture(ingest)))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_encoder_already_sending_is_offered_one_tap_announce():
    """OBS is already sending to this node (the HLS playlist answers) but nothing is announced: Streams shows a
    banner, and "Announce live" publishes the live event with the title typed into it."""
    ingest = {"enabled": True, "rtmp_url": "rtmp://fixture.invalid/live", "stream_key": "key123", "token": "tok123",
              "hls_url": "https://fixture.invalid/hls/tok123/index.m3u8"}

    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.switchView('streams'); 1")
        await b.until("!!document.querySelector('#ingest-banner #ib-go')")
        await b.js("document.querySelector('#ib-title').value='Already rolling'; document.querySelector('#ib-go').click(); 1")
        live = "__sent.find(e=>e.kind===30311&&e.tags.some(t=>t[0]==='status'&&t[1]==='live'))"
        await b.until(f"!!{live}")
        assert await b.js(f"{live}.tags.find(t=>t[0]==='title')[1]") == "Already rolling"
    asyncio.run(desktop.with_browser('online', '', check, fixture(ingest)))
