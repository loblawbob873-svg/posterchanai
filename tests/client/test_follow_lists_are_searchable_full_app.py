"""Profile -> Following / Followers can be SEARCHED, across the whole list.

"My Profile -> Following/Followers -> no way to search through it" (2026-10-17). The list drew the first 400 and
loaded profiles for the first 300, so somebody further down could not be found at all. The search covers every key;
the profiles not loaded yet are fetched behind the first paint so a name past both limits is still findable.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


N = 600
RELAY = r'''
function __mk(){ if(window.__made) return window.__made; const NT=window.NostrTools, now=Math.floor(Date.now()/1000);
  const sk=i=>{const b=new Uint8Array(32); b[29]=1; b[30]=(i>>8)&255; b[31]=i&255; return b;};
  const owner=sk(%(N)d+7), keys=Array.from({length:%(N)d},(_,i)=>sk(i+1)), pks=keys.map(k=>NT.getPublicKey(k));
  const k3=NT.finalizeEvent({kind:3,created_at:now-100,tags:pks.map(p=>['p',p]),content:''},owner);
  const prof={}; keys.forEach((k,i)=>{ prof[pks[i]]=NT.finalizeEvent({kind:0,created_at:now-50,tags:[],
    content:JSON.stringify({name:(i===550?'Zanzibar Quokka':'person '+i), nip05:'p'+i+'@example.com'})},k); });
  return (window.__made={owner:NT.getPublicKey(owner),k3,prof,pks}); }
class FakeSocket{constructor(u){this.url=u;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} if(m[0]!=='REQ')return; const id=m[1], D=__mk();
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  for(const f of m.slice(2)){
    if((f.kinds||[]).includes(3)&&(f.authors||[]).includes(D.owner)) out(['EVENT',id,D.k3]);
    if((f.kinds||[]).includes(0)&&f.authors) f.authors.slice(0,f.limit||500).forEach(a=>D.prof[a]&&out(['EVENT',id,D.prof[a]]));
  }
  setTimeout(()=>out(['EOSE',id]),10); }
 close(){this.readyState=3;} }
FakeSocket.OPEN=1;FakeSocket.CONNECTING=0;FakeSocket.CLOSED=3; window.WebSocket=FakeSocket;
''' % {"N": N}


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_following_is_searchable_past_what_is_drawn(width):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 860, "deviceScaleFactor": 1, "mobile": width < 821})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        owner = await b.js("__mk().owner")
        await b.js("__PC.openProfile('%s')" % owner)
        await b.until("!!document.querySelector('#show-following') && document.querySelector('#show-following b').textContent==='%d'" % N)
        await b.js("document.querySelector('#show-following').click()")
        await b.until("document.querySelectorAll('#people-list [data-prof]').length>0 && !!document.querySelector('#people-q')")
        got["drawn"] = await b.js("document.querySelectorAll('#people-list [data-prof]').length")
        await b.js("(()=>{const q=document.querySelector('#people-q');q.value='quokka';q.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await b.until("document.querySelectorAll('#people-list [data-prof]').length===1")
        got["hit"] = await b.js("document.querySelector('#people-list [data-prof] .pinfo b').textContent")
        got["count"] = await b.js("document.querySelector('#people-count').textContent")
        await b.js("(()=>{const q=document.querySelector('#people-q');q.value='nobody-like-this';q.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await b.until("/Nobody matches/.test(document.querySelector('#people-list').textContent)")
        # on a phone the field must fit the dialog: no sideways scroll, a finger-sized target
        got["fit"] = await b.js("(()=>{const q=document.querySelector('#people-q'),m=q.closest('.modal');const r=q.getBoundingClientRect(),mr=m.getBoundingClientRect();"
                                "return {inside:r.left>=mr.left-1&&r.right<=mr.right+1,h:Math.round(r.height),overflow:document.documentElement.scrollWidth>innerWidth+1}})()")

    asyncio.run(desktop.with_browser("online", "", check, RELAY))
    assert got["drawn"] == 400, got
    assert "Zanzibar Quokka" in got["hit"] and got["count"] == "1 of %d" % N, got
    assert got["fit"]["inside"] and not got["fit"]["overflow"] and got["fit"]["h"] >= 32, ("the search field does not fit the dialog", got)
