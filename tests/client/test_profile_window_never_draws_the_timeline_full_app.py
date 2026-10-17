"""A profile opened in its own window draws the profile -- never the social timeline first.

"opening a profile page from a thread opens up a social timeline for a split second, that is terrible and a waste
of resources" / "make sure we never open up a social page unless we open up the social page!" (2026-10-16). On
PosterChanOS a name clicked in a window opens `index.html?pcwin=doc:prof:<pk>`. A watcher installed before any app
script runs records whether the timeline's DOM is ever built or a timeline REQ ever leaves, on the way to the
profile.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


PK = "bb7eefb5bc81a7d438a764bb7c8e78799da50d09e505a419dbb7e5c0ff2e3d0d"

WATCH = r"""(()=>{window.__seen={timeline:0,tabs:0,profile:0,order:[],tlReq:0,titles:[]};
const mark=k=>{ if(!__seen[k]){ __seen.order.push(k); } __seen[k]++; };
const scan=()=>{ const vt=document.getElementById('view-title'); if(vt){ const x=(vt.textContent||'').trim(); if(__seen.titles[__seen.titles.length-1]!==x) __seen.titles.push(x); }
  if(document.getElementById('tl-notes')) mark('timeline');
  if(document.querySelector('.tl-tabs')&&document.querySelector('.tl-tabs').offsetParent) mark('tabs');
  if(document.querySelector('.prof-tab, #prof-list, .prof-head, .profile-head')) mark('profile'); };
new MutationObserver(scan).observe(document,{subtree:true,childList:true,attributes:true,attributeFilter:['class','style']});
let R;
Object.defineProperty(window,'Relay',{configurable:true,get(){return R;},set(v){R=v;
  for(const k of ['query','subscribe']){ const real=R[k]&&R[k].bind(R); if(!real) continue;
    R[k]=(filters,...rest)=>{ const fs=Array.isArray(filters)?filters:[filters];
      if(fs.some(f=>f&&(f.kinds||[]).includes(1)&&!f.authors&&!f.ids&&!f['#e']&&!f['#p'])) __seen.tlReq++;
      return real(filters,...rest); }; } }});
})();"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_profile_window_never_draws_the_timeline():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.call("Page.navigate", {"url": (await b.js("location.origin")) + "/index.html?pcwin=doc:prof:" + PK})
        await asyncio.sleep(1.5)
        await b.until("!!document.querySelector('.prof-tab, #prof-list')")
        await asyncio.sleep(2)
        got.update(await b.js("window.__seen"))

    asyncio.run(desktop.with_browser("online", "?pcwin=doc:prof:" + PK, check, WATCH))
    assert got["profile"], got
    assert not got["timeline"] and not got["tabs"], ("the profile window drew the social timeline", got)
    assert not got["tlReq"], ("the profile window asked the relays for a timeline it never shows", got)


THREAD = r"""(()=>{const now=Math.floor(Date.now()/1000);
  Store.saveEvent({id:'c'.repeat(64),kind:1,pubkey:'%s',created_at:now-50,tags:[],content:'the post',sig:''});
  Store.saveProfile({id:'9'.repeat(64),kind:0,pubkey:'%s',created_at:1,tags:[],content:JSON.stringify({name:'Thread Author'}),sig:''});
  __PC.openThread('c'.repeat(64)); return true;})()""" % (PK, PK)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1100, 390])
def test_a_profile_opened_from_a_thread_never_draws_the_timeline(width):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 821})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(THREAD)
        await b.until("!!document.querySelector('#feed [data-prof=\"%s\"]')" % PK)
        await asyncio.sleep(1)
        await b.js("window.__seen.timeline=0;window.__seen.tabs=0;window.__seen.tlReq=0;window.__seen.order=[];true")
        await b.js("document.querySelector('#feed [data-prof=\"%s\"]').click();true" % PK)
        await b.until("!!document.querySelector('.prof-tab, #prof-list')")
        await asyncio.sleep(2)
        got.update(await b.js("window.__seen"))

    asyncio.run(desktop.with_browser("online", "", check, WATCH))
    assert got["profile"], got
    assert not got["timeline"] and not got["tabs"], ("opening a profile from a thread drew the social timeline", got)
    assert not got["tlReq"], ("opening a profile from a thread asked for a timeline", got)



@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("view,want", [("doc:prof:" + PK, "Profile"), ("doc:post:" + "c" * 64, "Post"), ("mail", None)])
def test_a_window_never_starts_under_the_timelines_name(view, want):
    """Measured on the real desktop: a profile window's title read "Nostrverse" for ~2 s over an empty page."""
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.call("Page.navigate", {"url": (await b.js("location.origin")) + "/index.html?pcwin=" + view})
        await asyncio.sleep(4)
        got.update(await b.js("window.__seen"))

    asyncio.run(desktop.with_browser("online", "?pcwin=" + view, check, WATCH))
    titles = [t for t in got["titles"] if t]
    assert titles, got
    assert "Nostrverse" not in titles, ("the window was titled as the timeline while it booted", titles)
    if want:
        assert titles[0] == want, titles
