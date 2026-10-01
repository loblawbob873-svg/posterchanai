"""The real bundled client boots with timeline.js split out of app.js, and the timeline works.

Drives the SHIPPED desktop bundle in headless Chrome with a fake relay at the WebSocket boundary that
serves real signed kind-1 posts and keeps streaming new ones. What a broken split looks like:
  * timeline.js after app.js (or missing) -> built late by whichever forwarder is reached first, or
    a feed render handed a promise: caught by WHERE it was built (once, during app.js's evaluation,
    from _timelineMod -- never from _lzRun);
  * a dependency not passed, or a live binding captured by value -> the feed draws nothing, or live
    posts never arrive;
  * a shell that predates the tag (the APK takes index.html from the live site) -> the timeline must
    still draw, after the fallback load;
  * and no page or console error anywhere. Phone and desktop width.
"""
import asyncio
import hashlib
import json
import time
from pathlib import Path

import pytest

from app.services.nostr import bip340
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def _ev(sk, created, content):
    pk = bip340.pubkey_from_seckey(sk).hex()
    ser = json.dumps([0, pk, created, 1, [], content], separators=(",", ":"), ensure_ascii=False)
    eid = hashlib.sha256(ser.encode()).hexdigest()
    return {"id": eid, "pubkey": pk, "created_at": created, "kind": 1, "tags": [], "content": content,
            "sig": bip340.sign(bytes.fromhex(eid), sk).hex()}


_NOW = int(time.time())
_KEYS = [bytes([0] * 31 + [i + 7]) for i in range(6)]
EVENTS = [_ev(_KEYS[i % 6], _NOW - 60 - i * 30, f"older post number {i}") for i in range(60)]
LIVE = [_ev(_KEYS[i % 6], _NOW + 2 + i, f"live post number {i}") for i in range(10)]

RELAY = r'''
window.__EV=%s; window.__LIVE=%s;
window.__consoleErrors=[];
{const ce=console.error.bind(console);console.error=(...a)=>{__consoleErrors.push(a.map(x=>String(x&&x.stack||x)).join(' ').slice(0,300));ce(...a);};}
addEventListener('unhandledrejection',e=>__consoleErrors.push('unhandled: '+String(e.reason&&e.reason.stack||e.reason).slice(0,300)));
class FakeSocket{constructor(u){this.url=u;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} if(m[0]!=='REQ')return; const id=m[1], f=m[2]||{};
  const notes=(f.kinds||[]).includes(1) && !f.ids && !f.authors && !f['#e'] && !f['#p'];
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  if(notes){ const until=f.until||1e12; __EV.filter(e=>e.created_at<=until).slice(0,Math.min(f.limit||60,200)).forEach(e=>out(['EVENT',id,e]));
    out(['EOSE',id]);
    if(!f.until&&!window.__streaming){window.__streaming=true;let i=0;window.__liveTimer=setInterval(()=>{if(i<__LIVE.length)out(['EVENT',id,__LIVE[i++]]);},150);}
  } else out(['EOSE',id]); }
 close(){this.readyState=3;} }
FakeSocket.OPEN=1;FakeSocket.CONNECTING=0;FakeSocket.CLOSED=3; window.WebSocket=FakeSocket;
''' % (json.dumps(EVENTS), json.dumps(LIVE))

BUILD_PROBE = r'''
{let real, hidden=%s;Object.defineProperty(window,'PCTimelineFactory',{configurable:true,get(){return real;},set(f){
  if(hidden){ hidden=false; return; }          // a shell without the tag: the first copy never arrives
  real=function(dep){ window.__tlBuilds=(window.__tlBuilds||0)+1; window.__tlBuiltBeforePC=!window.__PC;
    window.__tlStack=String(new Error().stack); return f.apply(this,arguments); };}});}
'''


async def _timeline(b, width):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 600})
    await desktop.login(b)
    await b.js("__PC.switchView('global')")
    await b.until("document.querySelectorAll('#feed article').length>=20")
    text = await b.js("document.getElementById('feed').textContent")
    assert "older post number 0" in text, text[:300]
    # Live posts arrive while at the top of the feed.
    await b.until("document.getElementById('feed').textContent.includes('live post number')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_the_timeline_module_is_built_at_boot_and_the_feed_works(width):
    async def check(b):
        order = await b.js("[...document.scripts].map(s=>(s.getAttribute('src')||'').split('?')[0].split('/').pop())"
                           ".filter(n=>n==='timeline.js'||n==='app.js')")
        assert order == ["timeline.js", "app.js"], order
        assert await b.js("window.__tlBuilds") == 1
        assert await b.js("window.__tlBuiltBeforePC") is True
        stack = await b.js("window.__tlStack")
        assert "_timelineMod" in stack and "_lzRun" not in stack, stack
        await _timeline(b, width)
        assert await b.js("window.__tlBuilds") == 1
        assert await b.js("__consoleErrors") == []
    asyncio.run(desktop.with_browser("online", "", check, BUILD_PROBE % "false" + RELAY))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_shell_without_the_tag_still_draws_the_timeline():
    async def check(b):
        await b.until("window.__tlBuilds===1")          # built from the fallback load, not the tag
        await _timeline(b, 1280)
    asyncio.run(desktop.with_browser("online", "", check, BUILD_PROBE % "true" + RELAY))
