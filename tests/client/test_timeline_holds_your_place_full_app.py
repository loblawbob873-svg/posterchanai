"""What you are reading on the timeline stays put when something ABOVE it fills in late.

"i think image/video loading/link previews is making the timeline jumpy on mobile". Cards keep
growing after they are drawn -- a link preview fills in, an image's guessed box is corrected, a quoted
post arrives -- and #feed turns the browser's scroll anchoring off (the live prepend keeps its place
by hand), so every such change above the reader moved the post under their finger by its full
height. Measured on a 390px phone with slow images: 39px with the finger perfectly still.

Drives the SHIPPED client at phone size with posts that carry links whose previews answer late (the
real linkcards.js fill), scrolled into the middle of Global, and checks the first card on screen does
not move when: the previews above it fill in, a card straddling the top edge grows, and -- the other
half of the rule -- a card BELOW it grows (that one must not drag the page).
"""
import asyncio
import os
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client import test_effects_full_app as full

SOCKET = r'''
window.__posts=null;
function __buildPosts(){
 if(window.__posts) return window.__posts;
 const sk=new Uint8Array(32).fill(7), now=Math.floor(Date.now()/1000), out=[];
 for(let i=0;i<40;i++) out.push(NostrTools.finalizeEvent({kind:1,created_at:now-i*60,tags:[],
   content:'post number '+i+' with a link https://site'+i+'.example.test/article-'+i},sk));
 return window.__posts=out;
}
class FixtureSocket extends EventTarget{
 static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;__sockets.push(this);setTimeout(()=>{this.readyState=1;this.fire('open',{});},8);}
 fire(type,data){const e=type==='message'?new MessageEvent(type,{data:JSON.stringify(data)}):new Event(type);this['on'+type]?.(e);this.dispatchEvent(e);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;}if(!Array.isArray(m))return;
  if(m[0]==='REQ'){const sub=m[1],fs=m.slice(2);if(fs.some(f=>!f.kinds||f.kinds.includes(1)))for(const ev of __buildPosts())this.fire('message',['EVENT',sub,ev]);
   setTimeout(()=>this.fire('message',['EOSE',sub]),12);}
  if(m[0]==='EVENT')setTimeout(()=>this.fire('message',['OK',m[1].id,true,'']),12);}
 close(){if(this.readyState===3)return;this.readyState=3;this.fire('close',{});}
}
window.WebSocket=FixtureSocket;
// Link previews answer only when RELEASED, so they can land while the reader sits below them.
window.__previewGate=[];
{const of=window.fetch;window.fetch=(u,o)=>{const s=String(u);if(s.includes('/client/preview?')){
  return new Promise(res=>__previewGate.push(()=>res(new Response(JSON.stringify({title:'A preview title that wraps onto a second line on a phone',
    description:'A description long enough to take two or three lines of the card on a narrow screen.',site:'example'}),
    {status:200,headers:{'Content-Type':'application/json'}}))));}
  return of(u,o);};}
window.__release=()=>{const g=__previewGate.splice(0);g.forEach(f=>f());return g.length;};
window.__first=()=>{const f=document.getElementById('feed'),edge=f.getBoundingClientRect().top;
  for(const c of document.querySelectorAll('#tl-notes > *')){const r=c.getBoundingClientRect();if(r.top>=edge-1)return{key:c.dataset.key,top:Math.round(r.top)};}return null;};
'''
INIT = full.INIT.split("class FixtureSocket")[0] + SOCKET


async def run():
    full.ROOT = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-holdplace-", ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                                 "--window-size=390,800", "--remote-debugging-port=0", "--user-data-dir=" + profile,
                                 "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
            wsurl = next(p for p in pages if p.get("type") == "page")["webSocketDebuggerUrl"]
            async with websockets.connect(wsurl, max_size=20_000_000) as ws:
                b = full.Browser(ws)
                await b.call("Page.enable")
                await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
                await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 800, "deviceScaleFactor": 2, "mobile": True})
                await b.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": INIT})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value="
                           "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
                await b.until('!!__PC.me() && Relay.status==="ok"')
                await b.js("__PC.switchView('global')")
                await b.until("document.querySelectorAll('#tl-notes > *').length >= 30")
                await asyncio.sleep(.5)
                f = "document.getElementById('feed')"
                # Scroll down the way a reader does; the previews (fetched a few at a time, top first) for the
                # cards now ABOVE the reader are still outstanding.
                for _ in range(12):
                    await b.js(f"{f}.scrollTop += 350; true")
                    await asyncio.sleep(.25)
                await b.until("__previewGate.length >= 3")
                await asyncio.sleep(.4)
                out = {"before": await b.js("__first()"), "top0": await b.js(f"{f}.scrollTop")}
                # 1. The previews of every card above the reader answer now.
                out["released"] = 0
                for _ in range(4):                                   # a few rounds: they are fetched 4 at a time
                    out["released"] += await b.js("__release()")
                    await asyncio.sleep(.35)
                out["after_previews"] = await b.js("__first()")
                # 2. The card straddling the top edge grows (an image corrected to its real shape).
                await b.js(f"""(()=>{{const edge={f}.getBoundingClientRect().top;
                    const c=[...document.querySelectorAll('#tl-notes > *')].find(n=>{{const r=n.getBoundingClientRect();return r.top<edge&&r.bottom>edge;}});
                    c.style.paddingBottom='120px';}})()""")
                await asyncio.sleep(.3)
                out["after_straddle"] = await b.js("__first()")
                # 3. A card BELOW the first visible one grows: the page must not be dragged for it.
                top_before_below = await b.js(f"{f}.scrollTop")
                await b.js("""(()=>{const a=__first();const cards=[...document.querySelectorAll('#tl-notes > *')];
                    const i=cards.findIndex(c=>c.dataset.key===a.key);cards[i+1].style.paddingBottom='200px';})()""")
                await asyncio.sleep(.3)
                out["below_moved_scroll"] = (await b.js(f"{f}.scrollTop")) - top_before_below
                out["after_below"] = await b.js("__first()")
                # 4. Scroll back UP over the cards whose previews filled in while they were off screen. A GUARD,
                #    not a reproduction: measured, the old code moved exactly with the finger here too (the
                #    cards keep their size while content-visibility skips them). What it pins is that the
                #    compensation never moves the page by more than the scroll -- an over-correction.
                out["up"] = []
                for _ in range(8):
                    ref = await b.js("""(()=>{const a=__first();const c=[...document.querySelectorAll('#tl-notes > *')].find(n=>n.dataset.key===a.key);
                        window.__ref=c;return Math.round(c.getBoundingClientRect().top);})()""")
                    st = await b.js(f"{f}.scrollTop")
                    await b.js(f"{f}.scrollTop -= 250; true")
                    await asyncio.sleep(.3)
                    st2 = await b.js(f"{f}.scrollTop")
                    ref2 = await b.js("Math.round(__ref.getBoundingClientRect().top)")
                    out["up"].append({"scrolled": round(st - st2), "content_moved": ref2 - ref})
                return out
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            server.shutdown()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_post_you_are_reading_does_not_move():
    o = asyncio.run(run())

    a = o["before"]
    assert a and o["top0"] > 300 and o["released"] >= 10, o
    for step in ("after_previews", "after_straddle", "after_below"):
        b = o[step]
        assert b["key"] == a["key"] and abs(b["top"] - a["top"]) <= 2, (step, "the post under the reader moved", o)
    assert abs(o["below_moved_scroll"]) <= 1, ("a card BELOW the reader dragged the page", o)
    for step in o["up"]:
        assert abs(step["content_moved"] - step["scrolled"]) <= 2, ("scrolling up, the page moved more than the finger did", o["up"])
