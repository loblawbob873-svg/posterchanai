"""Trending -> Hot paints from the instance's ranking (/client/trending, ~16 KB), never by pulling 3,000
reactions to the phone; with no answer from the instance it still ranks from the relay, as before.

Vyram's video (2026-10-16): "Couldn't reach your relays just now" after ~4 s on a phone -- the old query was
2.2 MB of kind 6/7 events.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def _relay(endpoint_ok):
    return r'''
window.__reqs=[];
function __mk(){ if(window.__made) return window.__made; const NT=window.NostrTools, now=Math.floor(Date.now()/1000);
  const k=Uint8Array.from({length:32},(_,j)=>j===31?9:0), r=Uint8Array.from({length:32},(_,j)=>j===31?11:0);
  const notes=[]; for(let i=0;i<30;i++) notes.push(NT.finalizeEvent({kind:1,created_at:now-600-i,tags:[],content:'hot post '+i},k));
  const likes=[]; for(let i=0;i<5;i++) likes.push(NT.finalizeEvent({kind:7,created_at:now-60-i,tags:[['e',notes[3].id]],content:'+'},r));
  return (window.__made={notes,likes}); }
const __of=window.fetch;
window.fetch=async function(url,opts){ const u=String(url);
  if(u.includes('/client/trending')){ (window.__trendAsks=(window.__trendAsks||0)+1);
    if(!%s) return new Response('{"error":"unavailable"}',{status:503,headers:{'Content-Type':'application/json'}});
    const D=__mk(); return new Response(JSON.stringify({window:86400,rows:D.notes.slice(0,25).map((n,i)=>[n.id,50-i,'❤️'])}),{status:200,headers:{'Content-Type':'application/json'}}); }
  return __of(url,opts); };
class FakeSocket{constructor(u){this.url=u;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} if(m[0]!=='REQ')return; const id=m[1], D=__mk();
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  for(const f of m.slice(2)){ __reqs.push(f);
    if(f.ids){ D.notes.filter(n=>f.ids.includes(n.id)).forEach(n=>out(['EVENT',id,n])); }
    else if((f.kinds||[]).includes(7)){ D.likes.forEach(e=>out(['EVENT',id,e])); }
  }
  setTimeout(()=>out(['EOSE',id]),5); }
 close(){this.readyState=3;} }
FakeSocket.OPEN=1;FakeSocket.CONNECTING=0;FakeSocket.CLOSED=3; window.WebSocket=FakeSocket;
''' % ('true' if endpoint_ok else 'false')


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("endpoint_ok", [True, False])
def test_hot_is_the_instances_ranking(endpoint_ok):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 412, "height": 900, "deviceScaleFactor": 1, "mobile": True})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("try{localStorage.setItem('trTab','hot')}catch(_){}; __PC.switchView('trending')")
        await b.until("document.querySelectorAll('#tl-notes .tr-item').length>0 || !!document.querySelector('#tr-retry')")
        got["items"] = await b.js("document.querySelectorAll('#tl-notes .tr-item').length")
        got["first"] = await b.js("(document.querySelector('#tl-notes .tr-heat')||{}).textContent||''")
        got["asked"] = await b.js("window.__trendAsks||0")
        got["reaction_reqs"] = await b.js("__reqs.filter(f=>(f.kinds||[]).includes(7)&&!f.ids&&!f['#e']&&f.limit===3000).length")

    asyncio.run(desktop.with_browser("online", "", check, _relay(endpoint_ok)))
    assert got["asked"] >= 1, got
    if endpoint_ok:
        assert got["items"] >= 10 and "50" in got["first"], ("Trending did not paint the instance's ranking", got)
        assert got["reaction_reqs"] == 0, ("the phone still pulled every reaction to rank them itself", got)
    else:
        assert got["items"] >= 1 and "5" in got["first"], ("with no ranking from the instance, Trending stopped working", got)
