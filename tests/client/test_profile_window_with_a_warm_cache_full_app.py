"""A profile opened in its OWN WINDOW, with a warm cache, still shows the author's real history.

Reported on PosterChanOS: "bunny's timeline on profile is messed up still" -- after the gap fixes had
shipped and a fresh browser showed her profile complete. Measured in the real desktop app: the profile
window listed 20 replies from Jul 25 to Oct 7 with 18-day holes while the relay held 122 in that span.
The difference was the CACHE: a window boots with whatever the timeline left in IndexedDB, paints that
first (cache-first), and the refresh behind it never replaced it. This boots exactly that: a profile
window whose Store already holds 22 scattered posts by the author, against a relay that answers.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


PK = "bb7eefb5bc81a7d438a764bb7c8e78799da50d09e505a419dbb7e5c0ff2e3d0d"   # a real key: a fake one is not a valid point and routes elsewhere

# Before any app script runs: the Store is handed a warm cache of 22 posts spread over ~70 days (what the
# timeline leaves behind), and the relay answers this author's pages from a full history.
INIT = r"""(()=>{const PK='%s', now=Math.floor(Date.now()/1000), H=3600;
const ev=(i,t)=>({id:(''+i).padStart(64,'0'),kind:1,pubkey:PK,created_at:t,content:(i%%20?'reply ':'post ')+i,
  tags:i%%20?[['e','f'.repeat(64),'','root'],['p','d'.repeat(64)]]:[],sig:''});
window.__relay=[]; for(let i=0;i<600;i++) __relay.push(ev(i, now-i*H));
const cache=[]; for(let k=0;k<22;k++) cache.push(ev(2000+k, now-(k*3+1)*86400));
let S, R;
Object.defineProperty(window,'Store',{configurable:true,get(){return S;},set(v){S=v;
  try{ cache.forEach(e=>S.saveEvent(e)); S.saveProfile({id:'9'.repeat(64),kind:0,pubkey:PK,created_at:1,tags:[],content:JSON.stringify({name:'Warm Cache'}),sig:''}); }catch(_){ } }});
Object.defineProperty(window,'Relay',{configurable:true,get(){return R;},set(v){R=v;
  const real=R.query.bind(R); window.__asked=[];
  R.query=async(filters,...rest)=>{ const f=filters&&filters[0]||{};
    if(f.authors&&f.authors[0]===PK&&(f.kinds||[]).includes(1)){ __asked.push({until:f.until||null,limit:f.limit});
      await new Promise(r=>setTimeout(r,300));
      const out=__relay.filter(e=>!f.until||e.created_at<=f.until).slice(0,f.limit||80); out.complete=true; return out; }
    return real(filters,...rest); }; }});
})();""" % PK

GAPS = r"""(()=>{const ts=[...document.querySelectorAll('#prof-list .note, #prof-list article')]
  .map(n=>{const e=Store.get(n.dataset.id); return e?e.created_at:null;}).filter(Boolean);
  let max=0; for(let i=1;i<ts.length;i++) max=Math.max(max, Math.abs(ts[i-1]-ts[i]));
  return {n:ts.length, maxGapHours:Math.round(max/3600)};})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_profile_window_with_a_warm_cache_shows_the_relays_history_not_the_cache():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        # The window's ?pcwin= is consumed by the first boot, which ran BEFORE the login; open the
        # profile window again now that a session exists -- the way the desktop opens one.
        await b.call("Page.navigate", {"url": (await b.js("location.origin")) + "/index.html?pcwin=doc:prof:" + PK})
        await asyncio.sleep(1.5)
        await b.until("!!document.querySelector('.prof-tab[data-tab=\"replies\"]')")
        await asyncio.sleep(3)
        await b.js("document.querySelector('.prof-tab[data-tab=\"replies\"]').click();true")
        await asyncio.sleep(1.5)
        got["replies"] = await b.js(GAPS)
        got["asked"] = await b.js("window.__asked||[]")

    asyncio.run(desktop.with_browser("online", "?pcwin=doc:prof:" + PK, check, INIT))
    assert got["asked"], ("the profile window never asked the relay for the author's posts", got)
    r = got["replies"]
    assert r["n"] >= 30 and r["maxGapHours"] <= 2, ("the window kept the scattered cache instead of the relay's history", got)
