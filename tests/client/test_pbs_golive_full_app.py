"""ONE BUTTON TO NOSTR: PBS goes live with nothing to copy, shows the chat, reports the encoder, and Stop ends it.

Run: venv-unified/bin/python -m pytest tests/client/test_pbs_golive_full_app.py

"the goal of PBS is to simplify nostr streaming." The real bundled client, a real WebRTC session against a second
RTCPeerConnection standing in for the media server (tests/client/pbs_fixture.py), the relay fixture recording every
signed event. Driven with real clicks:

  token fetch   Go Live asks THIS instance for the account's ingest (/api/streams/ingest) -- no key on screen
  WHIP          the offer carries a video AND an audio m-line, and the far end receives both tracks: the composite
  30311 live    signed and published with status `live`, `streaming` = the instance's HLS url, the title, the
                description and the tags typed into PBS
  bitrate caps  the video sender is capped with setParameters at the Standard preset (1500 kbps, the node's clamp
                ceiling) and 30 fps; picking Saver while live re-caps it at 900 kbps
  encoder       the status bar says what getStats says: `hardware (VaapiVideoEncodeAccelerator)` when the
                encoder reports powerEfficientEncoder, `software (OpenH264)` when it does not
  chat          a viewer's kind-1311 message to the stream shows in PBS's chat panel
  Stop          a WHIP DELETE of the session the server named, the 30311 republished `ended`, the button back to
                Go Live
  elsewhere     a stream ended from the Streams page is shown as ended in PBS too (a closed PeerConnection fires
                no event, so PBS asks streams.js)
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import pbs_fixture as F
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


TAGS = "(e,k)=>e.tags.filter(t=>t[0]===k).map(t=>t[1])"

STATS = r"""(impl,hw)=>{const g=RTCPeerConnection.prototype.getStats;RTCPeerConnection.prototype.getStats=async function(){
  const r=await g.apply(this,arguments);const m=new Map();r.forEach((v,k)=>{const x=Object.assign({},v);
  if(x.type==='outbound-rtp'&&x.kind==='video'){x.encoderImplementation=impl;x.powerEfficientEncoder=hw;}m.set(k,x);});return m;};return true}"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_one_button_go_live_publishes_the_composite_and_stop_ends_it():
    got = {}

    async def check(b):
        await F.open_pbs(b, 1280)
        # a viewer already said hello in the stream's chat (the fixture relay serves it to the chat's REQ)
        await b.js("""(()=>{const k=new Uint8Array(32).fill(7);window.__events=(window.__events||[]).concat([
            NostrTools.finalizeEvent({kind:1311,created_at:Math.floor(Date.now()/1000),content:'hello from a viewer',
              tags:[['a','30311:'+__PC.me().pubkey+':tok123-1','','root']]},k)]);return true})()""")
        await F.click(b, '#pbs-add-text')
        await F.click(b, '#pbs-add-screen')
        await F.click(b, '#pbs-mic-on')
        await asyncio.sleep(.5)
        for sel, v in (('#pbs-title', 'Friday night PBS'), ('#pbs-summary', 'Testing the studio'), ('#pbs-tags', 'retro games, music')):
            await b.js("(()=>{const e=document.querySelector(%r);e.value=%r;e.dispatchEvent(new Event('input'));return true})()" % (sel, v))
        got['key_on_screen'] = await b.js("document.querySelector('#pbs').innerText.includes('k123')")
        await b.js('(' + STATS + ")('VaapiVideoEncodeAccelerator',true)")
        await F.click(b, '#pbs-go')
        await b.until("['live','error'].includes(document.querySelector('#pbs-status').dataset.state)")
        got['state'] = await b.js("document.querySelector('#pbs-status').dataset.state")
        got['why'] = await b.js("document.querySelector('#pbs-why').textContent")
        await b.until("__srvTracks.length>=2")
        await asyncio.sleep(3)
        got['ingest'] = await b.js('__ingestCalls')
        got['offer'] = await b.js("__whip[0]||''")
        got['tracks'] = await b.js('__srvTracks')
        got['live'] = await b.js("__published.filter(e=>e.kind===30311).map(e=>({s:(%s)(e,'status')[0],u:(%s)(e,'streaming')[0],t:(%s)(e,'title')[0],sum:(%s)(e,'summary')[0],tags:(%s)(e,'t'),img:(%s)(e,'image')[0]}))" % ((TAGS,) * 6))
        got['params'] = await b.js('__setParams')
        await b.until("document.querySelector('#pbs-enc').dataset.hw==='hardware'")
        got['enc_hw'] = await b.js("document.querySelector('#pbs-enc').textContent")
        await F.click(b, '.pbs-seg[data-seg="kbps"] .pbs-chip[data-v="900"]')
        await asyncio.sleep(.5)
        got['params_saver'] = await b.js('__setParams.slice(-1)[0]')
        await b.js('(' + STATS + ")('OpenH264',false)")
        await b.until("document.querySelector('#pbs-enc').dataset.hw==='software'")
        got['enc_sw'] = await b.js("document.querySelector('#pbs-enc').textContent")
        await b.until("document.querySelector('#pbs-chat-msgs').textContent.includes('hello from a viewer')")
        # the streamer answers in the chat from PBS itself
        await b.js("(()=>{const i=document.querySelector('#pbs-chat-inp');i.value='thanks for watching';i.dispatchEvent(new Event('input'));return true})()")
        await F.click(b, '#pbs-chat-send')
        await b.until("__published.some(e=>e.kind===1311&&e.content==='thanks for watching')")
        got['chat_sent'] = await b.js("__published.filter(e=>e.kind===1311&&e.content==='thanks for watching').map(e=>e.tags.find(t=>t[0]==='a')[1])")
        got['timer'] = await b.js("document.querySelector('#pbs-timer').textContent")
        got['kbps'] = await b.js("document.querySelector('#pbs-kbps').textContent")
        await F.shot(b, 'golive-live-1280.png')
        n_before = await b.js("__published.filter(e=>e.kind===30311).length")
        await F.click(b, '#pbs-go')
        await b.until("document.querySelector('#pbs-status').dataset.state==='offline'")
        await asyncio.sleep(.5)
        got['deleted'] = await b.js('__whipDel')
        got['after'] = await b.js("__published.filter(e=>e.kind===30311).slice(%d).map(e=>(%s)(e,'status')[0])" % (n_before, TAGS))
        got['button'] = await b.js("document.querySelector('#pbs-go').textContent.trim()")
        # live again, then ended ELSEWHERE (the Streams page's own End button): PBS must not go on saying LIVE
        await F.click(b, '#pbs-go')
        await b.until("document.querySelector('#pbs-status').dataset.state==='live'")
        await b.js("__PC.switchView('streams');true")
        await b.until("!!document.querySelector('#stream-end')")
        await b.js("document.querySelector('#stream-end').click();true")
        await asyncio.sleep(3.5)
        await b.js("__PC.switchView('pbs');true")
        await b.until("!!document.querySelector('#pbs-status')")
        got['elsewhere'] = await b.js("({s:document.querySelector('#pbs-status').dataset.state,w:document.querySelector('#pbs-why').textContent,b:document.querySelector('#pbs-go').textContent.trim()})")
        got['errors'] = await b.js('__errors')
    asyncio.run(desktop.with_browser('online', '', check, F.init()))
    assert got['errors'] == [], got['errors']
    assert not got['key_on_screen'], 'the stream key must not be needed (or shown) to go live'
    assert got['state'] == 'live', (got['state'], got['why'])
    assert got['ingest'] >= 1, 'the token must come from the instance'
    assert 'm=video' in got['offer'] and 'm=audio' in got['offer'], 'the WHIP offer must carry the composite video and the mix'
    assert sorted(got['tracks'])[:2] == ['audio', 'video'], got['tracks']
    live = [e for e in got['live'] if e['s'] == 'live']
    assert live, got['live']
    first = live[0]
    assert first['u'] == F.HLS and first['t'] == 'Friday night PBS' and first['sum'] == 'Testing the studio', first
    assert first['tags'] == ['retrogames', 'music'], first
    assert got['params'] and got['params'][0][0]['maxBitrate'] == 1500000 and got['params'][0][0]['maxFramerate'] == 30, got['params']
    assert got['params_saver'][0]['maxBitrate'] == 900000, ('changing the preset live did not re-cap the sender', got['params_saver'])
    assert 'hardware' in got['enc_hw'] and 'VaapiVideoEncodeAccelerator' in got['enc_hw'], got['enc_hw']
    assert 'software' in got['enc_sw'] and 'OpenH264' in got['enc_sw'], got['enc_sw']
    assert got['timer'] != '0:00', got['timer']
    assert got['chat_sent'] and got['chat_sent'][0].startswith('30311:'), ('a chat reply from PBS must go to the stream', got['chat_sent'])
    assert got['deleted'] and got['deleted'][0].endswith('/api/streams/whip/tok123/' + F.SID), ('Stop did not DELETE the WHIP session', got['deleted'])
    assert 'ended' in got['after'], ('Stop did not publish the 30311 ended', got['after'])
    assert got['button'].lower() == 'go live', got['button']
    assert got['elsewhere']['s'] == 'offline' and 'ended' in got['elsewhere']['w'] and got['elsewhere']['b'] == 'Go Live', \
        ('a stream ended elsewhere still shows as live in PBS', got['elsewhere'])
