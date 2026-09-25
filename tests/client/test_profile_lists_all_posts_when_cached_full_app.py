"""A profile the cache knows ONE post of still lists the author's posts -- no reload needed.

Reported: "profile says 3766 posts but profile on desktop says 1 … I had to reload his profile to see
the notes". A profile with anything cached paints from the cache first and refreshes behind it, and the
refresh's retry loop broke out as soon as the cache held any post by that author -- so when the first
notes query came back empty because no relay had finished (`complete === false`, the ordinary state of a
just-busy socket), the one cached post was the whole list until a reload. Reproduced exactly: one cached
post of five, first answer incomplete and empty, second answer whole.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
           "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")

SETUP = r'''(async()=>{const me=__PC.me(); window.__all=[];
  for(let i=0;i<5;i++){ __all.push(await __PC.signTemplate({kind:1,pubkey:me.pubkey,
    created_at:Math.floor(Date.now()/1000)-i*60,tags:[],content:'bot post '+i})); }
  Store.saveEvent(__all[0]);                                   // the cache knows ONE of them
  const q=Relay.query.bind(Relay); window.__noteQueries=0;
  Relay.query=async(f,...r)=>{
    const s=JSON.stringify(f);
    if(s.includes('"authors":["'+me.pubkey+'"]') && s.includes('"kinds":[1,1068,6]')){
      __noteQueries++;
      const a = __noteQueries===1 ? [] : __all.slice();
      a.complete = __noteQueries!==1;                          // first: no relay finished
      return a;
    }
    return q(f,...r);
  };
  return me.pubkey})()'''

COUNT = "document.querySelectorAll('#prof-list article, #prof-list .note').length"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_cached_profile_lists_every_post_once_the_relays_answer():
    got = {}

    async def check(b):
        await desktop.login(b)
        pk = await b.js(SETUP)
        await b.js(f"__PC.openProfile('{pk}')")
        await b.until("!!document.querySelector('#prof-list')")
        for _ in range(40):                                    # up to ~8s for the refresh to land
            if await b.js(COUNT) >= 5:
                break
            await asyncio.sleep(.2)
        got['listed'] = await b.js(COUNT)
        got['queries'] = await b.js("__noteQueries")

    asyncio.run(desktop.with_browser('online', '', check, CLASSIC))
    assert got['listed'] >= 5, (f"the profile listed {got['listed']} of 5 posts after the relays answered "
                                f"({got['queries']} notes queries) -- the cached one alone")
