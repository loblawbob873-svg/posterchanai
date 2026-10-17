"""The follower seed is incremental, and the live follower subscription never replays history.

The relay's main query cost on 2026-10-16 (py-spy, 40% of samples): {"#p":[me],"kinds":[3],"limit":150} and the seed's
limit:1000 -- full contact lists, each thousands of p-tags, 346 ms per request for one busy account, asked by EVERY
window on EVERY start and every reconnect (nine PosterChanOS windows = nine times). Two boots of the real client
against a relay that records each follower request: the first may seed fully; the second asks only for what changed.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


RELAY = r'''
window.__k3=[];
class FakeSocket{constructor(u){this.url=u;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} if(m[0]!=='REQ')return; const id=m[1];
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  for(const f of m.slice(2)) if((f.kinds||[]).includes(3)&&f['#p']) window.__k3.push({since:f.since||null,limit:f.limit||null});
  setTimeout(()=>out(['EOSE',id]),5); }
 close(){this.readyState=3;} }
FakeSocket.OPEN=1;FakeSocket.CONNECTING=0;FakeSocket.CLOSED=3; window.WebSocket=FakeSocket;
'''


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_second_start_asks_only_for_what_changed():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js("localStorage.setItem('pc_follow_seen', JSON.stringify({['f'.repeat(64)]: 1000}));true")   # one follower already known
        for _ in range(40):   # the seed retries until a complete read; then the live subscription follows it
            if await b.js("!!localStorage.getItem('pc_follow_seed_at:'+__PC.me().pubkey)"):
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(1)
        got["first"] = await b.js("window.__k3")
        await b.call("Page.reload", {})
        await asyncio.sleep(8)
        got["second"] = await b.js("window.__k3")

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    second = got["second"]
    assert second, ("the second start asked for no followers at all", got)
    full = [r for r in second if not r["since"]]
    assert not full, ("the second start pulled the whole follower history again", second)
    assert any(r["limit"] == 1000 for r in second), ("the incremental seed never ran", second)
