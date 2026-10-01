"""A busy Global timeline does not re-style and re-write every card for every arrival.

Reported: "posterchan still slow" on an Oukitel WP55 Pro, on the Global timeline, "slightly better with
autoload new posts off". Measured on the real poster.place Global at 412px with the CPU slowed 6x: 8.5 of
the first 10 seconds blocked on opening, and 5.7 of 10 while live posts arrived -- style recalculation,
pre-paint and layout of the WHOLE feed (150+ cards, nearly all off screen) on every insert at the top,
plus decorateProfiles rewriting every card's avatar/handle/name with identical values on every profile
batch (an identical `textContent` is still a new text node; an identical `src` re-runs the image update).
With both fixed: 3.3 s on opening and roughly a third of the cost per arriving post.

These assert the two causes directly, so they do not depend on how fast the machine running them is.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# 240 posts by 12 authors; their kind-0s arrive 700ms AFTER the cards are drawn, as on a cold feed.
RELAY = r'''
window.__made=null;
function __mk(){ if(window.__made) return window.__made; const NT=window.NostrTools, now=Math.floor(Date.now()/1000), o=location.origin;
  const keys=Array.from({length:12},(_,i)=>Uint8Array.from({length:32},(_,j)=>j===31?i+7:(j===30?1:0)));
  const ev=[]; for(let i=0;i<240;i++) ev.push(NT.finalizeEvent({kind:1,created_at:now-60-i*40,tags:[],content:'post number '+i},keys[i%12]));
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

NAMED = "[...document.querySelectorAll('#feed .note .name[data-prof]')].filter(n=>/^person\\d+$/.test(n.textContent.trim())).length"


async def _global(b):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": 412, "height": 900, "deviceScaleFactor": 1, "mobile": True})
    await desktop.login(b)
    await b.js("__PC.switchView('global')")
    await b.until("document.querySelectorAll('#feed .note').length>=40")
    # The profiles land after the cards: decoration must still fill them in.
    await b.until(NAMED + " >= 40")
    await b.until("[...document.querySelectorAll('#feed .note .av')].filter(a=>/icon-192\\.png\\?a=/.test(a.getAttribute('src')||'')).length>=40")
    await asyncio.sleep(.6)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_decorate_pass_over_unchanged_profiles_writes_nothing():
    got = {}

    async def check(b):
        await _global(b)
        got["named"] = await b.js(NAMED)
        got["mutations"] = await b.js(r"""(async()=>{const seen=[];const mo=new MutationObserver(ms=>{for(const m of ms)seen.push(m.type+':'+(m.attributeName||'')+':'+(m.target.className||m.target.nodeName))});
          mo.observe(document.getElementById('feed'),{subtree:true,childList:true,attributes:true,characterData:true});
          for(let i=0;i<3;i++) __PC.decorateProfiles();
          await new Promise(r=>setTimeout(r,50)); mo.disconnect(); return seen;})()""")

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    assert got["named"] >= 40, got
    assert got["mutations"] == [], ("a pass over unchanged profiles rewrote the feed", len(got["mutations"]), got["mutations"][:8])


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_offscreen_cards_are_skipped_and_onscreen_ones_are_drawn():
    got = {}

    async def check(b):
        await _global(b)
        got.update(await b.js(r"""(()=>{const feed=document.getElementById('feed'),fr=feed.getBoundingClientRect();
          const cards=[...document.querySelectorAll('#tl-notes > .note, #tl-notes > .reply-pair')];
          // content-visibility skips a card's CONTENTS, not the card: ask a child.
          const inner=c=>c.querySelector('.name')||c.firstElementChild;
          const cv=getComputedStyle(cards[0]).contentVisibility;
          const vis=c=>{const r=c.getBoundingClientRect();return r.bottom>fr.top&&r.top<fr.bottom};
          const on=cards.filter(vis), off=cards.filter(c=>{const r=c.getBoundingClientRect();return r.top>fr.bottom+2000});
          return {cards:cards.length,on:on.length,off:off.length,
            cv, onSkipped:on.filter(c=>!inner(c).checkVisibility({contentVisibilityAuto:true})).length,
            offSkipped:off.filter(c=>!inner(c).checkVisibility({contentVisibilityAuto:true})).length,
            onHeights:on.map(c=>Math.round(c.getBoundingClientRect().height))}})()"""))
        # Scrolling a skipped card into view draws it.
        got["drawnWhenReached"] = await b.js(r"""(async()=>{const cards=[...document.querySelectorAll('#tl-notes > .note')];const c=cards[cards.length-5];
          c.scrollIntoView();await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));await new Promise(r=>setTimeout(r,200));
          return (c.querySelector('.name')||c.firstElementChild).checkVisibility({contentVisibilityAuto:true}) && c.textContent.includes('post number')})()""")

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    assert got["on"] >= 2 and got["onSkipped"] == 0, ("a card on screen is not drawn", got)
    assert all(h > 40 for h in got["onHeights"]), got
    assert got["off"] >= 10 and got["offSkipped"] == got["off"], ("off-screen cards are still rendered", got)
    assert got["drawnWhenReached"] is True, got
