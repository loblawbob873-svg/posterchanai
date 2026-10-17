"""The always-on DM (gift-wrap) subscription asks for the last 7 days, not the whole inbox, on every reconnect.

server1, 2026-10-17: 250 DM inbox replays in 5 minutes. The live subscription had no `since` (gift wraps carry
randomised past timestamps) and is re-sent on every reconnect, so each phone wake replayed up to 500 wraps. NIP-59
bounds the randomisation at two days; 7 is the margin. The one-shot history read still asks with no `since`, so
old conversations still load.
"""
import asyncio
import time
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


RELAY = r'''
window.__reqs=[];
class FakeSocket{constructor(u){this.url=u;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} if(m[0]!=='REQ')return; m.slice(2).forEach(f=>__reqs.push(f));
  setTimeout(()=>{ if(this.readyState===1&&this.onmessage) this.onmessage({data:JSON.stringify(['EOSE',m[1]])}); },10); }
 close(){this.readyState=3;} }
FakeSocket.OPEN=1;FakeSocket.CONNECTING=0;FakeSocket.CLOSED=3; window.WebSocket=FakeSocket;
'''


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_live_gift_wrap_subscription_is_bounded_and_history_is_not():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; __PC.switchView('messages')")
        await b.until("__reqs.some(f=>(f.kinds||[]).includes(1059)&&f.limit===400)")
        await asyncio.sleep(1)
        got["wraps"] = await b.js("__reqs.filter(f=>(f.kinds||[]).includes(1059)&&!f['#k'])")

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    now = int(time.time())
    live = [f for f in got["wraps"] if "limit" not in f]
    history = [f for f in got["wraps"] if f.get("limit") == 400]
    assert live, ("no live gift-wrap subscription", got)
    assert all("since" in f and now - 8 * 86400 <= f["since"] <= now - 6 * 86400 for f in live), \
        ("the live gift-wrap subscription asks for the whole inbox", live)
    assert history and all("since" not in f for f in history), ("the history read lost older conversations", history)
