"""Pictures far off screen are put down and picked up again before they come back (PCImgPark).

"vyram said his apk social feed got worse ... choppy scroll, you see black on the timeline" (2026-10-16). Measured on
an emulated Pixel 5 (4x CPU), one fling through the Nostrverse feed with no instance (no server thumbnails): 765-823 MB
of decoded pictures held, every one for as long as its card exists -- past what a phone's WebView keeps rasterised, so
a fast scroll draws bare background. With parking: 11 MB after 83 cards. Needs no server, so it holds for an app run
with no instance, on any relays.

Asserted here on a fixed feed (240 posts, a picture each): after flinging far down, only pictures near the screen hold
decoded data; flinging back, every picture on screen is the real one again; and no card moves on its own.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


RELAY = r'''
window.__made=null;
function __mk(){ if(window.__made) return window.__made; const NT=window.NostrTools, now=Math.floor(Date.now()/1000), o=location.origin;
  const keys=Array.from({length:12},(_,i)=>Uint8Array.from({length:32},(_,j)=>j===31?i+7:(j===30?1:0)));
  const ev=[]; for(let i=0;i<240;i++) ev.push(NT.finalizeEvent({kind:1,created_at:now-60-i*40,tags:[],content:'post number '+i+' '+o+'/static/icon-512.png?n='+i},keys[i%12]));
  const prof={}; keys.forEach((k,i)=>{ const pk=NT.getPublicKey(k); prof[pk]=NT.finalizeEvent({kind:0,created_at:now-9999,tags:[],
    content:JSON.stringify({name:'person'+i,picture:o+'/static/icon-192.png?a='+i,nip05:'person'+i+'@example.com'})},k); });
  return (window.__made={ev,prof}); }
class FakeSocket{constructor(u){this.url=u;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} if(m[0]!=='REQ')return; const id=m[1];
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});}; const D=__mk();
  for(const f of m.slice(2)){
    if((f.kinds||[]).includes(0)&&f.authors){ setTimeout(()=>{ f.authors.forEach(a=>D.prof[a]&&out(['EVENT',id,D.prof[a]])); out(['EOSE',id]); },700); return; }
    if((f.kinds||[]).includes(1)&&!f.ids&&!f.authors&&!f['#e']&&!f['#p']){ const until=f.until||1e12;
      D.ev.filter(e=>e.created_at<=until).slice(0,Math.min(f.limit||60,100)).forEach(e=>out(['EVENT',id,e])); out(['EOSE',id]); return; }
  }
  out(['EOSE',id]); }
 close(){this.readyState=3;} }
FakeSocket.OPEN=1;FakeSocket.CONNECTING=0;FakeSocket.CLOSED=3; window.WebSocket=FakeSocket;
'''

GIF = "data:image/gif"
HELD = "[...document.querySelectorAll('#tl-notes img')].filter(i=>i.complete&&i.naturalWidth>1&&/icon-512/.test(i.currentSrc||'')).length"
SEEN = "document.querySelectorAll('#tl-notes .note').length"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_far_pictures_are_put_down_and_come_back():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 412, "height": 900, "deviceScaleFactor": 1, "mobile": True})
        await desktop.login(b)
        await b.js("__PC.switchView('global')")
        await b.until("document.querySelectorAll('#feed .note').length>=40")
        await b.until("[...document.querySelectorAll('#tl-notes img')].filter(i=>/icon-512/.test(i.getAttribute('src')||'')).length>=20")
        got["far"] = await b.js(r"""(async()=>{const f=document.getElementById('feed'),sleep=ms=>new Promise(r=>setTimeout(r,ms));
          let loaded=0;
          for(let i=0;i<70;i++){ f.scrollBy({top:600,behavior:'instant'}); await sleep(150);
            for(const im of document.querySelectorAll('#tl-notes img')) if(/icon-512/.test(im.currentSrc||'')) im.dataset.everLoaded='1'; }
          await sleep(900);
          return {cards:document.querySelectorAll('#tl-notes .note').length,
                  ever:document.querySelectorAll('#tl-notes img[data-ever-loaded]').length,
                  held:""" + "[...document.querySelectorAll('#tl-notes img')].filter(i=>i.complete&&i.naturalWidth>1&&/icon-512/.test(i.currentSrc||'')).length" + r"""};})()""")
        got["back"] = await b.js(r"""(async()=>{const f=document.getElementById('feed'),sleep=ms=>new Promise(r=>setTimeout(r,ms));
          let missing=0, jumps=0, checked=0;
          for(let i=0;i<70;i++){ f.scrollBy({top:-600,behavior:'instant'}); await sleep(150);
            const c=[...document.querySelectorAll('#tl-notes .note')].find(c=>{const r=c.getBoundingClientRect();return r.top>=0&&r.top<innerHeight/2;});
            if(c){const t0=c.getBoundingClientRect().top; await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
              if(Math.abs(c.getBoundingClientRect().top-t0)>2) jumps++;}
            await sleep(250);
            for(const im of document.querySelectorAll('#tl-notes img')){const r=im.getBoundingClientRect();
              if(r.bottom>0&&r.top<innerHeight&&r.height>0){checked++; if(String(im.getAttribute('src')||'').startsWith('data:image/gif')) missing++;}}}
          return {missing, jumps, checked};})()""")

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    far, back = got["far"], got["back"]
    assert far["cards"] >= 90 and far["ever"] >= 80, ("the feed did not load enough to measure", far)
    assert far["held"] <= 45, ("pictures far off screen are still decoded", far)
    assert back["checked"] >= 40, back
    assert back["missing"] == 0, ("a picture on screen is still the placeholder", back)
    assert back["jumps"] == 0, ("a card moved on its own", back)
