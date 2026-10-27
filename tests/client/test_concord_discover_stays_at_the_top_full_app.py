"""Communities' Discover stays at the TOP while the public communities load.

Git issue "Discover Community Loading Jerk Screen" (Vyram, 2026-10-27, with a video): opening Communities, the page
showed "Find your community", then the public listings arrived and the screen jerked DOWN to the end of the list,
"Find your community" and its buttons scrolled away. Discover is painted in the same scroller as a chat, and a chat's
reader is pinned to the newest message -- so the listings' arrival "grew the room" and pinned Discover to its bottom.

The shipped client at phone size, no communities joined, the directory answered by a stub relay door after a delay
(the real parser reads an Armada-shaped directory post): "Find your community" must still be on screen, unmoved, once
the listings are drawn -- including for a reader whose earlier visit this session stored the pinned state.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

LABELS = ["Community %d" % i for i in range(20)]
CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')"
           "||'{}'),osMode:false}));")
DOOR = r"""
(()=>{const labels=%s, urls=labels.map((_,i)=>'https://armada.buzz/invite/naddr1'+String.fromCharCode(97+i)+'qq#BAADAwQBfixture'+i);
  const post={id:'d'.repeat(64),pubkey:'e'.repeat(64),kind:1,created_at:Math.floor(Date.now()/1000),tags:[['t','concord'],['t','community']],
    content:'Armada now has a Discover page.\n\n'+labels.map((n,i)=>n+': '+urls[i]).join('\n\n'),sig:'0'.repeat(128)};
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  // The directory is read through the app's own Relay.queryFrom (concord.js readDirectory), which counts a relay as
  // answered only when its `report` says so. Patched once the client has defined it.
  const patch=()=>{ if(!window.Relay||!Relay.queryFrom){ setTimeout(patch,20); return; }
    const real=Relay.queryFrom.bind(Relay);
    Relay.queryFrom=async(urls,filters,o)=>{
      if(!(filters||[]).some(f=>(f.kinds||[]).includes(1)&&/invite/.test(f.search||''))) return real(urls,filters,o);
      await wait(1200);
      if(o&&o.report){ o.report.ok=(o.report.ok||[]).concat(urls); }
      return [post]; }; };
  patch();
})();
""" % json.dumps(LABELS)
WHERE = r"""(()=>{const h=[...document.querySelectorAll('.cc-discover h2')].find(e=>/Find your community/.test(e.textContent));
  const box=h&&h.closest('.cc-messages');
  return {h2:h?Math.round(h.getBoundingClientRect().top):null, top:box?Math.round(box.scrollTop):null,
    rows:document.querySelectorAll('.cc-public-room').length};})()"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("stored", [False, True])
def test_the_listings_arrive_without_moving_the_page(stored):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 850, "deviceScaleFactor": 1, "mobile": True})
        await desktop.login(b)
        if stored:   # an earlier visit this session stored the old default (pinned) for this very screen
            await b.js("Object.keys(sessionStorage).length; for(const k of ['general','']) sessionStorage.setItem("
                       "'pc.concord.scroll.'+__PC.me().pubkey+':home:'+k, JSON.stringify({top:0,height:0,pinned:true})); 1")
        await b.js("__PC.switchMessagesTab('concord'); 1")
        await b.until("[...document.querySelectorAll('.cc-discover h2')].some(e=>/Find your community/.test(e.textContent))")
        await asyncio.sleep(.2)
        got["before"] = await b.js(WHERE)
        for _ in range(100):
            if (await b.js(WHERE))["rows"] >= 15:
                break
            await asyncio.sleep(.1)
        await asyncio.sleep(2.0)       # every delayed pin/restore pass (enterChatBottom's last is 1.6s)
        got["after"] = await b.js(WHERE)
        got["errors"] = await b.js("(window.__errors||[]).slice(-5)")

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC + DOOR))
    before, after = got["before"], got["after"]
    assert after["rows"] >= 15, ("the listings never arrived", got)
    assert after["top"] == 0, ("Discover jerked down when the listings arrived", got)
    assert after["h2"] == before["h2"], ("'Find your community' moved", got)
    assert not got["errors"], got["errors"]
