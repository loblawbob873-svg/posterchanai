"""A post with several pictures shows them TWO at a time, and scrolling right shows the next two.

"for social timeline, can you show images 2 at a time and then you can scroll to the right to see the other 2 and so
on?" (2026-10-16). It was one picture per view ("1/5"). Each picture fills its half of a fixed-height strip, the
counter and dots say which pair is showing, the arrow pages by two, and the strip's height never depends on the bytes.
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
  const ev=[]; for(let i=0;i<240;i++) ev.push(NT.finalizeEvent({kind:1,created_at:now-60-i*40,tags:[],content:(i===0?'five pictures '+[1,2,3,4,5].map(k=>o+'/static/icon-512.png?p='+k).join(' '):'post number '+i)},keys[i%12]));
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

STATE = r"""(()=>{const car=document.querySelector('#tl-notes .media-car');if(!car)return null;const tr=car.querySelector('.mc-track');
  const vis=[...tr.children].map(c=>c.getBoundingClientRect()).map((r,i)=>({i,l:Math.round(r.left),w:Math.round(r.width),t:Math.round(r.top),h:Math.round(r.height)}))
    .filter(r=>r.l>=tr.getBoundingClientRect().left-2&&r.l+r.w<=tr.getBoundingClientRect().right+2);
  return {count:car.querySelector('.mc-count').textContent,vis:vis.map(v=>v.i),widths:vis.map(v=>v.w),tops:vis.map(v=>v.t),heights:vis.map(v=>v.h),
    track:Math.round(tr.clientWidth),next:car.querySelector('.mc-next').disabled,prev:car.querySelector('.mc-prev').disabled,
    dots:[...car.querySelectorAll('.mc-dots i')].map(d=>d.classList.contains('on')?1:0).join('')};})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [412, 1280])
def test_pictures_show_two_at_a_time(width):
    got = []

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 821})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('global')")
        await b.until("!!document.querySelector('#tl-notes .media-car')")
        await b.js("document.querySelector('#tl-notes .media-car').scrollIntoView({block:'center'})")
        await asyncio.sleep(1.2)
        got.append(await b.js(STATE))
        for _ in range(2):
            await b.js("document.querySelector('#tl-notes .media-car .mc-next').click()")
            await asyncio.sleep(1.0)
            got.append(await b.js(STATE))

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    first, second, third = got
    assert first["vis"] == [0, 1], ("not two pictures in view", first)
    assert abs(first["widths"][0] - first["widths"][1]) <= 2 and first["widths"][0] * 2 <= first["track"] + 4, first
    assert first["tops"][0] == first["tops"][1] and first["heights"][0] == first["heights"][1], ("side by side, same strip", first)
    assert first["count"] == "1\u20132/5" and first["dots"] == "11000" and first["prev"] and not first["next"], first
    assert second["vis"] == [2, 3] and second["count"] == "3\u20134/5", second
    assert third["vis"] == [3, 4] and third["count"] == "4\u20135/5" and third["next"], ("the last pair, and nowhere further", third)
