"""Live streams: the buttons no test pressed, each pressed in the SHIPPED bundle and checked for what it says it does.

  * Go Live's cover image: '#gl-img-pick' opens your drive and the image tapped becomes the cover (and is what
    the announced kind-30311 carries as `image`); '#gl-img-clear' clears it.
  * The camera broadcast's overlay ('#phone-live'): '#pl-min' shrinks it to the thumbnail (the broadcast keeps
    running); '#pl-chat' shrinks it AND opens Streams; '#pl-flip' swaps the outgoing camera for the other one
    (asked for with `exact`, replaced on the live sender, the preview un-mirrored); '#pl-screen' swaps the camera
    for a screen capture and back; '#pl-stop' ends the broadcast (sender closed, every capture stopped, the
    30311 re-signed `ended`).
  * While live, Edit details' '#gl-det-x' closes the sheet and publishes nothing.
  * '#st-del' on your own stream: Cancel does nothing, OK publishes a NIP-09 kind 5 naming the stream's address
    and event, and goes back to Streams.

Only the network (ingest, WHIP answer, relay), the camera/screen (canvas tracks) and the PeerConnection are
fixtures; a headless browser has no camera and no WHIP server.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


INGEST = {"enabled": True, "rtmp_url": "rtmp://fixture.invalid/live", "stream_key": "key123", "token": "tok123",
          "hls_url": "https://fixture.invalid/hls/tok123/index.m3u8", "whip_url": "https://fixture.invalid/whip/tok123"}

FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__toasts=[];
new MutationObserver(ms=>ms.forEach(m=>m.addedNodes.forEach(n=>{if(n.classList&&n.classList.contains('toast'))__toasts.push(n.textContent);}))).observe(document,{childList:true,subtree:true});
window.__whip=[];window.__drive=[];
const __gf=window.fetch;
window.fetch=(u,o)=>{const s=String(u),json=(v,st=200)=>Promise.resolve(new Response(JSON.stringify(v),{status:st,headers:{'Content-Type':'application/json'}}));
  if(s.includes('/api/streams/ingest'))return json(INGEST);
  if(s.includes('/whip/')){__whip.push(o&&o.body);return Promise.resolve(new Response('v=0\r\nfixture-answer\r\n',{status:201}));}
  if(s.includes('/api/streams/viewers/'))return json({viewers:2});
  if(s.includes('/list/'))return json(__drive);
  if(s.includes('/hls/')||s.includes('.m3u8'))return Promise.resolve(new Response('#EXTM3U\n',{status:200}));
  return __gf(u,o);};
// The camera and the screen: real MediaStreamTracks from a canvas, each labelled with what was asked for.
window.__gum=[];window.__gdm=0;window.__tracks=[];
const __stream=(src)=>{const c=document.createElement('canvas');c.width=320;c.height=240;const g=c.getContext('2d');
  g.fillStyle='#246';g.fillRect(0,0,320,240);const s=c.captureStream(5);
  s.getTracks().forEach(t=>{t.__src=src;__tracks.push(t);});return s;};
navigator.mediaDevices.getUserMedia=async c=>{__gum.push(JSON.parse(JSON.stringify(c)));
  // A camera is found by facing or by its deviceId ('cam-<facing>'), and reports both like a real one.
  const v=c.video||{}, f=v.facingMode, id=v.deviceId&&(v.deviceId.exact||v.deviceId.ideal||v.deviceId);
  const want=(typeof f==='string'?f:(f&&(f.exact||f.ideal)))||(id?String(id).replace(/^cam-/,''):'');
  const s=__stream('cam:'+want);
  s.getVideoTracks().forEach(t=>{t.getSettings=()=>({deviceId:'cam-'+want,facingMode:want});});
  if(c.audio){const ac=new AudioContext(),d=ac.createMediaStreamDestination(),o=ac.createOscillator();o.connect(d);o.start();
    d.stream.getAudioTracks().forEach(t=>{t.__src='mic';__tracks.push(t);s.addTrack(t);});}
  return s;};
navigator.mediaDevices.getDisplayMedia=async()=>{__gdm++;return __stream('screen');};
navigator.mediaDevices.enumerateDevices=async()=>[];
// The WHIP PeerConnection: records what it sends and what replaced it.
window.__pcs=[];
window.RTCPeerConnection=class{
  constructor(cfg){this.cfg=cfg;this.senders=[];this.iceGatheringState='complete';this.connectionState='new';this.closed=false;__pcs.push(this);}
  addTrack(t){const snd={track:t,replaced:[],getParameters(){return {encodings:[{}]};},async setParameters(){},
    async replaceTrack(nt){this.replaced.push(nt);this.track=nt;}};this.senders.push(snd);return snd;}
  getSenders(){return this.senders;} getTransceivers(){return [];}
  async createOffer(){return {type:'offer',sdp:'v=0\r\nfixture-offer\r\n'};}
  async setLocalDescription(d){this.localDescription=d;}
  async setRemoteDescription(d){this.remoteDescription=d;this.connectionState='connected';}
  addEventListener(){} removeEventListener(){}
  close(){this.closed=true;this.connectionState='closed';}};
'''.replace('INGEST', json.dumps(INGEST))

TOASTS = "__toasts.join(' | ')"
LIVE = "__published.filter(e=>e.kind===30311&&e.tags.some(t=>t[0]==='status'&&t[1]==='live'))"
SENT_VIDEO = "__pcs[0].senders.find(s=>s.track&&s.track.kind==='video').track"
MINI = "document.querySelector('#phone-live').classList.contains('pl-mini')"
VIEW = "document.querySelector('#view-title').textContent"


async def click(b, sel):
    await b.until(f"!!document.querySelector({json.dumps(sel)})")
    await b.js(f"document.querySelector({json.dumps(sel)}).click(); 1")


@CHROME
def test_cover_pick_and_clear_then_the_broadcast_overlay_buttons():
    async def check(b):
        await desktop.login(b)
        await b.js("window.__publishOK=true; __drive=[{sha256:'c'.repeat(64),url:location.origin+'/static/icon-192.png',type:'image/png',uploaded:1}];"
                   " __PC.switchView('streams'); 1")
        await click(b, '#stream-golive')
        await b.until("!!document.querySelector('#gl-img-pick')")
        cover = await b.js("location.origin+'/static/icon-192.png'")
        CLEAR_HIDDEN = "document.querySelector('#gl-img-clear').classList.contains('hidden')"
        assert await b.js("document.querySelector('#gl-img').value") == '' and await b.js(CLEAR_HIDDEN)

        # Choose from your drive: the drive opens, and the image tapped becomes the cover.
        await click(b, '#gl-img-pick')
        await b.until(f"!!document.querySelector('.bp-cell[data-url={json.dumps(cover)}]')")
        await b.js(f"document.querySelector('.bp-cell[data-url={json.dumps(cover)}]').click(); 1")
        await b.until(f"document.querySelector('#gl-img').value==={json.dumps(cover)} && !{CLEAR_HIDDEN} && !document.querySelector('.bp-modal')")
        # Clear: the cover is gone, and so is the Clear button.
        await click(b, '#gl-img-clear')
        await b.until(f"document.querySelector('#gl-img').value==='' && {CLEAR_HIDDEN}")
        # Pick it again and go live from the front camera: the 30311 carries that cover.
        await click(b, '#gl-img-pick')
        await b.until(f"!!document.querySelector('.bp-cell[data-url={json.dumps(cover)}]')")
        await b.js(f"document.querySelector('.bp-cell[data-url={json.dumps(cover)}]').click(); 1")
        await b.until(f"document.querySelector('#gl-img').value==={json.dumps(cover)}")
        await b.js("""(()=>{const pick=(n,v)=>{const r=document.querySelector(`input[name=${n}][value=${v}]`);r.checked=true;r.dispatchEvent(new Event('change',{bubbles:true}));};
            pick('gl-src','cam'); pick('gl-facing','user');
            const a=document.querySelector('#gl-announce'); if(a) a.checked=false;
            document.querySelector('#gl-title').value='Overlay buttons';})(); 1""")
        await b.until("!document.querySelector('#gl-go').disabled")
        await click(b, '#gl-go')
        await b.until(f"!!document.querySelector('#phone-live') && __pcs.length===1 && !!__pcs[0].remoteDescription && {LIVE}.length>=1")
        live = (await b.js(LIVE))[0]
        assert ['image', cover] in live['tags'], live['tags']
        assert await b.js("__gum[0].video.facingMode") == 'user'
        assert await b.js(f"{SENT_VIDEO}.__src") == 'cam:user'

        # Desktop lands on the stream with the overlay as a thumbnail; tapping it brings it back.
        await b.until(MINI)
        await b.js("document.querySelector('#phone-live').click(); 1")
        await b.until(f"!{MINI}")

        # Minimize: thumbnail again, still live (same sender, nothing stopped).
        await click(b, '#pl-min')
        await b.until(f"{MINI} && /still live/.test({TOASTS})")
        assert await b.js("!__pcs[0].closed && __tracks.every(t=>t.readyState==='live')")
        await b.js("document.querySelector('#phone-live').click(); 1")
        await b.until(f"!{MINI}")

        # Chat: from another screen, it shrinks the broadcast AND opens Streams.
        await b.js("__PC.switchView('home'); 1")
        await b.until(f"{VIEW}==='Home'")
        await click(b, '#pl-chat')
        await b.until(f"{MINI} && {VIEW}==='Streams'")
        assert await b.js("!__pcs[0].closed")
        await b.js("document.querySelector('#phone-live').click(); 1")
        await b.until(f"!{MINI}")

        # Flip camera: the REAR camera is asked for exactly and replaces the front one on the live sender.
        n = await b.js("__gum.length")
        await click(b, '#pl-flip')
        await b.until(f"{SENT_VIDEO}.__src==='cam:environment' && /rear camera/.test({TOASTS})")
        assert await b.js(f"__gum[{n}].video.facingMode") == {'exact': 'environment'}
        assert await b.js("document.querySelector('#pl-vid').classList.contains('rear')"), 'the rear camera is still mirrored'
        assert await b.js("__tracks.filter(t=>t.__src==='cam:user').every(t=>t.readyState==='ended')"), 'the front camera was left running'

        # Share screen: the screen replaces the camera; the button turns into "Camera" and Flip goes away.
        await click(b, '#pl-screen')
        await b.until(f"__gdm===1 && {SENT_VIDEO}.__src==='screen' && /Camera/.test(document.querySelector('#pl-screen').textContent)")
        assert await b.js("document.querySelector('#pl-flip').style.display") == 'none'
        # ...and pressed again, back to the camera it was on.
        await click(b, '#pl-screen')
        await b.until(f"{SENT_VIDEO}.__src==='cam:environment' && /Share screen/.test(document.querySelector('#pl-screen').textContent)")
        assert await b.js("document.querySelector('#pl-flip').style.display") == ''
        assert await b.js("__tracks.filter(t=>t.__src==='screen').every(t=>t.readyState==='ended')"), 'the screen capture was left running'

        # Edit details → Cancel: the sheet closes and nothing is re-signed.
        await b.js(f"__PC.openStream({LIVE}[0]); 1")
        await click(b, '#st-editdet')
        await b.until("!!document.querySelector('#gl-det-x') && !!document.querySelector('#gl-summary')")
        before = await b.js("__published.filter(e=>e.kind===30311).length")
        await b.js("document.querySelector('#gl-summary').value='never saved'; 1")
        await click(b, '#gl-det-x')
        await b.until("!document.querySelector('#gl-det-x')")
        await asyncio.sleep(1)
        assert not await b.js("__published.some(e=>e.kind===30311&&e.tags.some(t=>t[0]==='summary'&&t[1]==='never saved'))")
        # (the viewer poll may re-sign the headcount meanwhile; nothing else may be)
        assert await b.js(f"__published.filter(e=>e.kind===30311).slice({before}).every(e=>e.tags.some(t=>t[0]==='current_participants'))")

        # Stop streaming: the broadcast ends -- sender closed, every capture stopped, the 30311 says ended.
        await click(b, '#pl-stop')
        await b.until("!document.querySelector('#phone-live') && __pcs[0].closed"
                      " && __published.some(e=>e.kind===30311&&e.tags.some(t=>t[0]==='status'&&t[1]==='ended'))")
        assert await b.js("__tracks.every(t=>t.readyState==='ended')"), await b.js("__tracks.map(t=>t.__src+':'+t.readyState)")
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))


@CHROME
def test_delete_your_own_stream():
    async def check(b):
        await desktop.login(b)
        await b.js("""window.__publishOK=true;
          window.__mine={kind:30311,content:'',created_at:Math.floor(Date.now()/1000)-60,pubkey:__PC.me().pubkey,
            tags:[['d','tok123-1700000000'],['title','Old stream'],['status','ended']]};
          (async()=>{ const ev=await window.NostrTools.finalizeEvent(__mine, new Uint8Array(32).fill(1)); window.__mine=ev; __PC.openStream(ev); })(); 1""")
        await b.until("!!document.querySelector('#st-del')")
        sid = await b.js("__mine.id")
        pk = await b.js("__PC.me().pubkey")
        # Cancel: nothing is deleted.
        await click(b, '#st-del')
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"0\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"0\"]').click(); 1")
        await asyncio.sleep(.6)
        assert not await b.js("__published.some(e=>e.kind===5)")
        # OK: a kind 5 naming the stream's address and this event, and back to Streams.
        await click(b, '#st-del')
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click(); 1")
        await b.until(f"__published.some(e=>e.kind===5) && /stream deleted/.test({TOASTS}) && {VIEW}==='Streams'")
        tags = await b.js("__published.find(e=>e.kind===5).tags")
        assert ['a', f'30311:{pk}:tok123-1700000000'] in tags and ['e', sid] in tags and ['k', '30311'] in tags, tags
        assert not await b.js("__errors"), await b.js("__errors")
    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
