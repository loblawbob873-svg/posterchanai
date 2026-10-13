"""Coming back to the timeline puts you back on the POST you were reading -- measured as a rate.

"Feed not staying stationary" (Android APK, 384x748 @3.75, filed with its own scroll report). The
report's scroll writes read, in order:

    put < _putScroll < renderTimeline          0    -> 4131   the remembered PIXEL
    _restoreTlAnchor < put < _putAnchor          4131 -> 5674   the remembered POST, +1543
    _restoreTlAnchor < _drawTimeline < markEosed 5674 -> 6516   the EOSE redraw, +842

and the same `put < _putScroll < renderTimeline` after the composer closed. On a phone every timeline
card is `content-visibility:auto` with a 420px intrinsic size, so a timeline that was just redrawn is a
column of ESTIMATES until each card is laid out. Both restores compute scrollTop from that column:

  * the pixel (renderView(true) after posting, an outbox flush, a mute, a reconnect, or returning to
    the tab) lands among different posts, because a pixel of the old laid-out column means nothing in
    a column of estimates. Measured: the post being read ended up ~1,400px below the top of the screen.
  * the post anchor (Back from a post or a profile) is right for the estimates -- and wrong one frame
    later, when the cards it landed among are laid out for real. Those are FIRST size observations,
    which _holdReadingPlace skips by design (an insertion is not a resize), so nothing compensated:
    the post you had opened ended up ~750px ABOVE the screen, every time.

Every trial boots its own Chrome with a fresh profile at the reporter's viewport, scrolls into the
middle of Global, does one flow and checks that the card that was first on screen is first on screen
again, at the same offset, and stays there. Run more trials by hand with
    venv-unified/bin/python -m tests.client.test_timeline_keeps_your_post_full_app [trials] [flow ...]
"""
import asyncio
import json
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client import test_effects_full_app as full

CHROME = "/opt/google/chrome/chrome"
# Posts of very different heights, none of them the 420px estimate -- that difference is the bug.
SOCKET = r'''
window.__posts=null;
function __buildPosts(){
 if(window.__posts) return window.__posts;
 const sk=new Uint8Array(32).fill(7), now=Math.floor(Date.now()/1000), out=[];
 for(let i=0;i<80;i++) out.push(NostrTools.finalizeEvent({kind:1,created_at:now-i*60,tags:[],
   content:'timeline post '+i+'\n'+'a line of text\n'.repeat(i%7)},sk));
 return window.__posts=out;
}
class FixtureSocket extends EventTarget{
 static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;__sockets.push(this);setTimeout(()=>{this.readyState=1;this.fire('open',{});},8);}
 fire(type,data){const e=type==='message'?new MessageEvent(type,{data:JSON.stringify(data)}):new Event(type);this['on'+type]?.(e);this.dispatchEvent(e);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;}if(!Array.isArray(m))return;
  if(m[0]==='REQ'){const sub=m[1],fs=m.slice(2);
   setTimeout(()=>{ if(fs.some(f=>!f.kinds||f.kinds.includes(1)))for(const ev of __buildPosts()){
     if(fs.some(f=>(!f.until||ev.created_at<=f.until)&&(!f.since||ev.created_at>=f.since)&&(!f.ids||f.ids.includes(ev.id))&&(!f.authors||f.authors.includes(ev.pubkey))))this.fire('message',['EVENT',sub,ev]);}
     setTimeout(()=>this.fire('message',['EOSE',sub]),30);}, 40);}
  if(m[0]==='EVENT')setTimeout(()=>this.fire('message',['OK',m[1].id,true,'']),12);}
 close(){if(this.readyState===3)return;this.readyState=3;this.fire('close',{});}
}
window.WebSocket=FixtureSocket;
window.__first=()=>{const f=document.getElementById('feed'),edge=f.getBoundingClientRect().top;
  for(const c of document.querySelectorAll('#tl-notes > *')){const r=c.getBoundingClientRect();if(r.top>=edge-1)return{key:c.dataset.key,top:Math.round(r.top)};}return null;};
window.__topOf=k=>{const c=[...document.querySelectorAll('#tl-notes > *')].find(n=>n.dataset.key===k);return c?Math.round(c.getBoundingClientRect().top):null;};
'''
INIT = full.INIT.split("class FixtureSocket")[0] + SOCKET
F = "document.getElementById('feed')"
FLOWS = ("back_from_post", "back_from_profile", "repaint")


async def _trial(b, port, flow):
    await b.call("Page.navigate", {"url": f"http://127.0.0.1:{port}/client"})
    await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
    await b.until("document.body.classList.contains('guest')")
    await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value="
               "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
    await b.until('!!__PC.me() && Relay.status==="ok"')
    await b.js("__PC.switchView('global')")
    await b.until("document.querySelectorAll('#tl-notes > *').length >= 40")
    await asyncio.sleep(.8)
    for _ in range(12):                                     # read down the feed: every card passed is laid out
        await b.js(f"{F}.scrollTop += 300; true")
        await asyncio.sleep(.12)
    await asyncio.sleep(.6)
    before = await b.js("__first()")
    key = json.dumps(before["key"])
    card = f"[...document.querySelectorAll('#tl-notes > *')].find(n=>n.dataset.key==={key})"
    if flow == "back_from_post":
        await b.js(f"{card}.querySelector('.txt').click()")
        await b.until("location.pathname !== '/client' && !document.getElementById('tl-notes')")
        await asyncio.sleep(.6)
        await b.js("history.back()")
    elif flow == "back_from_profile":
        await b.js(f"{card}.querySelector('.name[data-prof]').click()")
        await b.until("!document.getElementById('tl-notes')")
        await asyncio.sleep(.6)
        await b.js("history.back()")
    elif flow == "repaint":                                 # renderView(true): after posting, an outbox flush, a mute…
        await b.js("__PC.retryInstanceView('global')")
    await b.until("document.querySelectorAll('#tl-notes > *').length >= 40")
    tops = []
    for _ in range(10):                                     # and it must STAY there, not just pass through
        await asyncio.sleep(.2)
        tops.append(await b.js(f"__topOf({key})"))
    after = await b.js("__first()")
    return {"flow": flow, "before": before, "after": after, "tops": tops}


def _ok(r):
    a, b = r["before"], r["after"]
    return bool(b) and b["key"] == a["key"] and all(t is not None and abs(t - a["top"]) <= 4 for t in r["tops"])


async def run(flow, trials):
    full.ROOT = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    out = []
    try:
        for _ in range(trials):
            with tempfile.TemporaryDirectory(prefix="pc-keeps-post-", ignore_cleanup_errors=True) as profile:
                proc = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                                         "--window-size=384,748", "--remote-debugging-port=0", "--user-data-dir=" + profile,
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
                        # The reporter's phone: SM-S908U1, 384x748 CSS px at dpr 3.75, touch.
                        await b.call("Emulation.setDeviceMetricsOverride", {"width": 384, "height": 748, "deviceScaleFactor": 3.75, "mobile": True})
                        await b.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
                        await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": INIT})
                        out.append(await _trial(b, server.server_port, flow))
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
    finally:
        server.shutdown()
    return out


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome required")
@pytest.mark.parametrize("flow", FLOWS)
def test_coming_back_puts_you_on_the_post_you_were_reading(flow):
    results = asyncio.run(run(flow, 3))
    bad = [r for r in results if not _ok(r)]
    assert not bad, (f"{flow}: {len(bad)}/{len(results)} trials put the reader somewhere else "
                     "(before = first card on screen; tops = that card's top afterwards)", bad)


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    for flow in sys.argv[2:] or FLOWS:
        rs = asyncio.run(run(flow, n))
        bad = [r for r in rs if not _ok(r)]
        print(f"{flow}: {len(bad)}/{len(rs)} failing")
        for r in bad[:2]:
            print("   ", json.dumps(r))
