"""Timeline cards do not keep a finished entry animation alive -- the cause of scroll jank while loading.

Reported (on a phone): "whenever the timeline is loading something the scrolling performance slugs".
Measured with a Chrome trace, scrolling down at 412px with the CPU slowed 4x while older pages and
pictures loaded: 18 main-thread stalls over 50ms (worst frame 283ms), dominated by Commit / Layerize /
Paint and 27,438 UpdateLayer events. Every `.note` carried `animation: noteRise … both`: a finished
animation with fill-mode `both` stays IN EFFECT, so ~100 cards each held a compositing layer the
browser re-layerised every frame. Drawing cards plainly gave 1 stall and a 17ms p99 frame.

This asserts the cause directly -- `getAnimations()` lists an animation that is still in effect -- so it
does not depend on how fast the machine is: after the first page, and after an older page loaded at
the bottom, no card holds an animation. Only a post that arrives LIVE at the top rises (pc-rise, set by
_prependLive), with fill-mode `backwards` so it lets go -- checked against the shipped CSS and code.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


RELAY = r'''
window.__made=null; window.__liveOn=false;
function __mk(){ if(window.__made) return window.__made; const NT=window.NostrTools, now=Math.floor(Date.now()/1000);
  const keys=Array.from({length:12},(_,i)=>Uint8Array.from({length:32},(_,j)=>j===31?i+7:(j===30?1:0)));
  const ev=[]; for(let i=0;i<240;i++) ev.push(NT.finalizeEvent({kind:1,created_at:now-60-i*40,tags:[],content:'post number '+i},keys[i%12]));
  const live=[]; for(let i=0;i<3;i++) live.push(NT.finalizeEvent({kind:1,created_at:now+5+i,tags:[],content:'live arrival '+i},keys[i]));
  return (window.__made={ev,live}); }
class FakeSocket{constructor(u){this.url=u;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;}
  if(m[0]==='CLOSE'){ (window.__open||new Map()).delete(m[1]); return; }
  if(m[0]!=='REQ')return; const id=m[1], f=m[2]||{};
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  const D=__mk();
  if((f.kinds||[]).includes(1)&&!f.ids&&!f.authors&&!f['#e']&&!f['#p']){ const until=f.until||1e12;
    D.ev.filter(e=>e.created_at<=until).slice(0,Math.min(f.limit||60,60)).forEach(e=>out(['EVENT',id,e])); out(['EOSE',id]);
    // Live posts go to every open, unbounded timeline subscription -- what a real relay does.
    if(!f.until){ (window.__open=window.__open||new Map()).set(id,out);
      window.__live=()=>{ for(const [sid,o] of window.__open) D.live.forEach(e=>o(['EVENT',sid,e])); }; }
    return; }
  out(['EOSE',id]); }
 close(){this.readyState=3;} }
FakeSocket.OPEN=1;FakeSocket.CONNECTING=0;FakeSocket.CLOSED=3; window.WebSocket=FakeSocket;
'''

HOLDING = "[...document.querySelectorAll('#feed .note')].filter(n=>n.getAnimations().length).length"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [412, 1280])
def test_cards_let_go_of_their_animation_and_only_live_posts_rise(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 600})
        await desktop.login(b)
        await b.js("__PC.switchView('global')")
        await b.until("document.querySelectorAll('#feed .note').length>=20")
        await asyncio.sleep(.8)
        first = await b.js("({cards:document.querySelectorAll('#feed .note').length, holding:%s})" % HOLDING)
        assert first["holding"] == 0, ("cards drawn on the first page still hold an animation", first)

        # Scroll to the bottom: an older page is drawn below, and holds nothing either.
        n0 = first["cards"]
        await b.js("(()=>{const f=document.getElementById('feed');f.scrollTop=f.scrollHeight;f.dispatchEvent(new Event('scroll'));})()")
        await b.until("document.querySelectorAll('#feed .note').length>%d" % n0)
        await asyncio.sleep(.8)
        assert await b.js(HOLDING) == 0, "cards from an older page hold an animation"

    asyncio.run(desktop.with_browser("online", "", check, RELAY))


def test_only_live_posts_rise_and_they_let_go():
    root = Path(__file__).resolve().parents[2]
    css = (root / "static/css/client.css").read_text(encoding="utf-8")
    rule = css[css.index(".note.pc-rise{"):][:120]
    assert "noteRise" in rule and "backwards" in rule and " both" not in rule, rule
    import re
    assert not re.search(r"(?m)^\.note\{[^}]*animation\s*:\s*noteRise", css), "every card animates again"
    from tests.client_source import client_source
    app = client_source()
    prepend = app[app.index("function _prependLive("):][:900]
    assert "classList.add('pc-rise')" in prepend
