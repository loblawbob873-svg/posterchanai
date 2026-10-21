"""Scrolling the Nostrverse at phone speed: no forced layout in the picture parking, no reset at start-up.

"we need to make sure pixel 5 performance is smooth for timeline scrolling ... vyram was still reporting
unsmoothness" (2026-10-20). Measured with a Pixel-5-class probe (393x851 at 2.75 DPR, 6x CPU, real touch
flings; ~/.cache/posterchan-work/scrollprobe.py): every run had 3-4 long tasks and one 265-330 ms stall. The
two causes, each fixed and asserted here:

  * PCImgPark's IntersectionObserver read `card.offsetHeight` for each card it parked, right after the previous
    card's writes -- a forced layout per card, 60-70 ms per callback, the biggest single cost (144 ms of the
    profile). The observer entry already carries the height;
  * the account's follows/mutes/pins arriving a few seconds after the feed painted ran `renderView(true)`, which
    on a timeline is a RESET: the older pages scrolled into were dropped and the cards rebuilt under the thumb.
    The Nostrverse does not depend on follows, so it is redrawn in place.

After both: no long tasks at 6x or 8x, the worst frame ~50 ms (was 330 ms).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_timeline_far_pictures_are_put_down_full_app import RELAY

needs_chrome = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# The account's follow list is answered only when the test says so: that is "hydration lands while the person
# is already scrolling", on cue.
HOLD_FOLLOWS = r'''
(()=>{const S=window.WebSocket, send=S.prototype.send; window.__heldFollows=[];
 S.prototype.send=function(raw){let m;try{m=JSON.parse(raw);}catch(_){return send.call(this,raw);}
  if(Array.isArray(m)&&m[0]==='REQ'&&m.slice(2).some(f=>(f.kinds||[]).includes(3))){
    const id=m[1],me=this; window.__heldFollows.push(()=>{if(me.onmessage)me.onmessage({data:JSON.stringify(['EOSE',id])});}); return;}
  return send.call(this,raw);};
 window.__releaseFollows=()=>{const q=window.__heldFollows.splice(0);q.forEach(f=>f());return q.length;};})();
'''

# Count every offsetHeight read made from inside PCImgPark's park(): each one is a forced layout.
SPY = r'''
(()=>{const d=Object.getOwnPropertyDescriptor(HTMLElement.prototype,'offsetHeight');window.__parkReads=0;
 Object.defineProperty(HTMLElement.prototype,'offsetHeight',{configurable:true,get(){
   if(/\bpark\b/.test(new Error().stack||''))window.__parkReads++; return d.get.call(this);}});})();
'''


async def fling(b, times):
    for _ in range(times):
        await b.js("(async()=>{const f=document.querySelector('#feed');for(let i=0;i<10;i++){f.scrollTop+=260;"
                   "await new Promise(r=>requestAnimationFrame(r));}})()")
        await asyncio.sleep(.35)


@needs_chrome
def test_parking_pictures_reads_no_layout():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 393, "height": 851, "deviceScaleFactor": 2.75, "mobile": True})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")   # the phone layout, not the windowed desktop
        await b.js("__PC.switchView('global')")
        await b.until("document.querySelectorAll('#feed .note').length>=40")
        await b.until("[...document.querySelectorAll('#tl-notes img')].filter(i=>i.complete&&i.naturalWidth>1).length>=15")
        await b.js(SPY)
        await fling(b, 10)
        await asyncio.sleep(1)
        got["reads"] = await b.js("window.__parkReads")
        got["parked"] = await b.js("""[...document.querySelectorAll('#tl-notes>[data-parked]')].map(c=>({pinned:parseFloat(c.style.height)||0}))""")

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    assert got["parked"], ("nothing was parked, so this measured nothing", got)
    assert all(p["pinned"] > 40 for p in got["parked"]), ("a parked card lost its height", got["parked"][:5])
    assert got["reads"] == 0, ("parking forced a layout per card (offsetHeight)", got["reads"])


@needs_chrome
def test_the_accounts_follows_arriving_do_not_reset_the_nostrverse_under_the_reader():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 393, "height": 851, "deviceScaleFactor": 2.75, "mobile": True})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")   # the phone layout, not the windowed desktop
        await b.js("__PC.switchView('global')")
        await b.until("document.querySelectorAll('#feed .note').length>=40")
        await b.until("window.__heldFollows.length>0")
        await fling(b, 6)
        await asyncio.sleep(1)
        await b.js("""(()=>{const f=document.querySelector('#feed'),edge=f.getBoundingClientRect().top;
          window.__cards=[...document.querySelectorAll('#tl-notes>[data-key]')];window.__cards.forEach(c=>c.__mine=1);
          window.__first=window.__cards.find(c=>c.getBoundingClientRect().bottom>edge);
          window.__firstTop=Math.round(window.__first.getBoundingClientRect().top-edge);})()""")
        got["before"] = await b.js("({cards:window.__cards.length, top:document.querySelector('#feed').scrollTop})")
        await b.js("""(()=>{window.__added=0;new MutationObserver(rs=>{for(const r of rs)window.__added+=r.addedNodes.length;})
          .observe(document.querySelector('#feed'),{childList:true,subtree:true});})()""")
        got["released"] = await b.js("window.__releaseFollows()")
        await asyncio.sleep(3)
        got["after"] = await b.js("""(()=>{const f=document.querySelector('#feed'),edge=f.getBoundingClientRect().top,
            now=[...document.querySelectorAll('#tl-notes>[data-key]')];
          return {added:window.__added, cards:now.length, kept:now.filter(c=>c.__mine).length, firstThere:window.__first.isConnected,
                  firstTop:window.__first.isConnected?Math.round(window.__first.getBoundingClientRect().top-edge):null,
                  wasTop:window.__firstTop};})()""")

    asyncio.run(desktop.with_browser("online", "", check, RELAY + HOLD_FOLLOWS))
    b, a = got["before"], got["after"]
    assert got["released"] > 0, ("the follow list was never asked for, so this measured nothing", got)
    assert a["cards"] >= b["cards"], ("loaded posts were dropped when the follows arrived", got)
    assert a["kept"] >= b["cards"] - 2, ("the cards were rebuilt when the follows arrived", got)
    assert a["firstThere"] and abs(a["firstTop"] - a["wasTop"]) <= 4, ("the post under the reader moved", got)
