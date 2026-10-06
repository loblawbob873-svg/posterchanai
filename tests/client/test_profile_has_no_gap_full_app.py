"""A profile's Replies list has no hole that is not on the relay -- and scroll-back fills from where it stops.

The owner: "there is no way I have an 11 day reply gap" (and the same on another account: "huge gap between
posts"). Measured on poster.place: 1,502 replies on the relay, 20-100 every single day, and an 11-day hole on
the profile. The profile showed the newest page it fetched, then whatever OLDER posts the local cache held
from the timeline weeks ago, and paged back from THOSE -- so the stretch between was never asked for.

Driven in the shipped bundle against a relay stub holding one post an hour for 25 days (every 20th top-level, the rest replies), with the cache
already holding a few from a month ago (as the timeline leaves it):
  * the Replies list is one unbroken run (no jump bigger than the relay's own spacing);
  * scroll-back asks for posts older than the oldest one FETCHED, not the oldest one cached;
  * the same holds when the profile is REOPENED and paints from the cache first, then refreshes;
  * the Posts tab of an author who mostly replies fills itself (pages on) instead of showing three posts
    -- a list too short to scroll can never reach the scroll-back trigger.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


PK = "c" * 64

STUB = r"""(()=>{
const PK='%s', now=Math.floor(Date.now()/1000), H=3600;
// Every 20th is a top-level post, the rest are replies -- an author who mostly replies.
const ev=(i,t)=>({id:(''+i).padStart(64,'0'),kind:1,pubkey:PK,created_at:t,content:(i%%20?'reply number ':'post number ')+i,
                  tags:i%%20?[['e','f'.repeat(64),'','root'],['p','d'.repeat(64)]]:[],sig:''});
window.__relay=[]; for(let i=0;i<600;i++) __relay.push(ev(i, now-i*H));        // one an hour, 25 days -- more than the test scrolls through
// What the timeline left in the cache a month ago.
for(let i=0;i<5;i++) Store.saveEvent(ev(1001+i, now-30*86400-i*H));   // replies (1000 would be a top-level post)
window.__asked=[];
const real=Relay.query.bind(Relay);
Relay.query=async(filters,...rest)=>{
  const f=filters&&filters[0]||{};
  if(f.authors&&f.authors[0]===PK && (f.kinds||[]).includes(1)){
    __asked.push({until:f.until||null,limit:f.limit});
    const out=__relay.filter(e=>!f.until||e.created_at<=f.until).slice(0,f.limit||80);
    out.complete=true; return out; }
  return real(filters,...rest); };
return true;})()""" % PK

GAPS = r"""(()=>{const ts=[...document.querySelectorAll('#prof-list .note, #prof-list article')]
  .map(n=>{const e=Store.get(n.dataset.id); return e?e.created_at:null;}).filter(Boolean);
  let max=0; for(let i=1;i<ts.length;i++) max=Math.max(max, Math.abs(ts[i-1]-ts[i]));
  return {n:ts.length, maxGapHours:Math.round(max/3600)};})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_replies_tab_has_no_hole_and_scroll_back_continues_from_what_was_fetched():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(STUB)
        await b.js("__PC.openProfile('%s');true" % PK)
        await b.until("!!document.querySelector('.prof-tab[data-tab=\"replies\"]')")
        await asyncio.sleep(.8)
        await b.js("document.querySelector('.prof-tab[data-tab=\"replies\"]').click();true")
        await asyncio.sleep(.6)
        got["first"] = await b.js(GAPS)
        # Scroll to the bottom: the scroll-back trigger.
        for _ in range(3):
            await b.js("(()=>{const f=document.getElementById('feed');f.scrollTop=f.scrollHeight;f.dispatchEvent(new Event('scroll'));})();true")
            await asyncio.sleep(.6)
        got["asked"] = await b.js("__asked")
        got["after"] = await b.js(GAPS)
        got["firstPageFloor"] = await b.js("Math.min(...__relay.slice(0,80).map(e=>e.created_at))")

    asyncio.run(desktop.with_browser("online", "", check))
    first, after, asked = got["first"], got["after"], got["asked"]
    assert first["n"] > 20 and first["maxGapHours"] <= 2, ("the Replies list jumps over a stretch the relay has", first)
    pages = [a for a in asked if a["until"]]
    assert pages, ("scroll-back never asked for older replies", asked)
    assert pages[0]["until"] == got["firstPageFloor"] - 1, ("scroll-back skipped from the oldest CACHED reply", pages[0], got)
    assert after["maxGapHours"] <= 2 and after["n"] >= first["n"], ("a hole opened after scrolling back", after)


async def _open(b):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 900, "deviceScaleFactor": 1, "mobile": False})
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js(STUB)
    await b.js("__PC.openProfile('%s');true" % PK)
    await b.until("!!document.querySelector('.prof-tab[data-tab=\"replies\"]')")
    await asyncio.sleep(.8)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_reopening_a_cached_profile_has_no_hole_once_it_refreshes():
    got = {}

    async def check(b):
        await _open(b)
        await b.js("__PC.switchView('global');true")
        await asyncio.sleep(.5)
        await b.js("__PC.openProfile('%s');true" % PK)          # the cache-first path this time
        await b.until("!!document.querySelector('.prof-tab[data-tab=\"replies\"]')")
        await asyncio.sleep(1.5)                                  # the refresh behind the cached paint
        await b.js("document.querySelector('.prof-tab[data-tab=\"replies\"]').click();true")
        await asyncio.sleep(.6)
        got["gaps"] = await b.js(GAPS)

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["gaps"]["n"] > 20 and got["gaps"]["maxGapHours"] <= 2, ("a reopened profile shows a hole", got)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_posts_tab_of_a_reply_heavy_author_fills_itself():
    got = {}

    async def check(b):
        await _open(b)
        await asyncio.sleep(2.5)                                  # automatic pages, no scrolling
        got["posts"] = await b.js(GAPS)
        got["pages"] = await b.js("__asked.filter(a=>a.until).length")

    asyncio.run(desktop.with_browser("online", "", check))
    p = got["posts"]
    assert p["n"] >= 8, ("the Posts tab stayed nearly empty instead of paging on", got)
    assert p["maxGapHours"] <= 21, ("the Posts tab skipped posts the relay has", got)
    assert got["pages"] >= 1, got


# THE GAP THE LIVE CHECK FOUND AFTER DEPLOY 116. On poster.place every kind-1 reply was on the profile and
# 638 replies were not: they were kind 1111 (NIP-22 comments on kind-1 posts, `K 1`) -- most of what this
# account replies with. The Replies tab already LISTED 1111 from the cache, but the profile never ASKED the
# relay for it, so the tab showed whichever 1111s the timeline happened to leave behind: an "11 day gap".
# This relay honours `kinds`, as a real one does.
STUB_1111 = r"""(()=>{
const PK='%s', now=Math.floor(Date.now()/1000), H=3600;
const ev=i=>i%%20 ? {id:(''+i).padStart(64,'0'),kind:1111,pubkey:PK,created_at:now-i*H,content:'comment number '+i,
    tags:[['E','f'.repeat(64)],['K','1'],['P','d'.repeat(64)],['e','f'.repeat(64)],['k','1'],['p','d'.repeat(64)]],sig:''}
  : {id:(''+i).padStart(64,'0'),kind:1,pubkey:PK,created_at:now-i*H,content:'post number '+i,tags:[],sig:''};
window.__relay=[]; for(let i=0;i<300;i++) __relay.push(ev(i));
window.__asked=[];
const real=Relay.query.bind(Relay);
Relay.query=async(filters,...rest)=>{
  const f=filters&&filters[0]||{};
  if(f.authors&&f.authors[0]===PK && (f.kinds||[]).includes(1)){
    __asked.push({kinds:f.kinds,until:f.until||null});
    const out=__relay.filter(e=>f.kinds.includes(e.kind) && (!f.until||e.created_at<=f.until)).slice(0,f.limit||80);
    out.complete=true; return out; }
  return real(filters,...rest); };
return true;})()""" % PK


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_replies_made_as_nip22_comments_are_on_the_replies_tab():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(STUB_1111)
        await b.js("__PC.openProfile('%s');true" % PK)
        await b.until("!!document.querySelector('.prof-tab[data-tab=\"replies\"]')")
        await asyncio.sleep(.8)
        await b.js("document.querySelector('.prof-tab[data-tab=\"replies\"]').click();true")
        await asyncio.sleep(.8)
        got["replies"] = await b.js(GAPS)
        got["asked"] = await b.js("__asked")

    asyncio.run(desktop.with_browser("online", "", check))
    assert any(1111 in a["kinds"] for a in got["asked"]), ("the profile never asked the relay for its NIP-22 comments", got["asked"])
    assert got["replies"]["n"] >= 20 and got["replies"]["maxGapHours"] <= 2, ("the Replies tab skipped the comments", got)
