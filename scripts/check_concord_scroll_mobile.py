#!/usr/bin/env python3
"""READING BACK THROUGH A ROOM ON A PHONE, MEASURED AS A POSITION OVER TIME.

Reported three times, most recently as "i enter room scroll up a little, and it jumps to the top".
Every previous fix was reasoned about and unit-simulated, and the simulations agreed with the code
rather than with the phone: a fake scroller has no momentum, no clamping, no real `offsetTop`, and
no layout, so it cannot see a jerk. So this drives the SHIPPED concord.js in a real headless Chrome
at a phone viewport, scrolls with REAL TOUCH FLINGS through CDP's own gesture synthesiser, and then
watches the position for seconds afterwards.

WHAT IT ASSERTS is not "the restore computed the right number" but "nothing moved the reader" —
a sampled position, because every one of these bugs is a SEQUENCE. Each scenario is one thing that
happens in a live room while somebody is reading backwards:

  settle    nothing happens at all; the entry burst must not still be firing
  hydrate   the room's history finishes loading seconds after entry (the real ordering: render()
            pins, hydration awaits, and the `finally` pins AGAIN — at which point the reader has
            been reading for two seconds)
  message   somebody posts while you are reading history
  growth    a picture above you finishes decrypting and the content gets taller

Exit 2 = could not run (no Chrome), never a pass.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PC_CHECK_PORT") or 9486)
DEBUG = PORT + 1000
WIDTH, HEIGHT = 390, 844

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="stylesheet" href="/static/css/client.css">
<link rel="stylesheet" href="/static/css/concord.css">
<style>html,body{width:100%;height:100%;margin:0}.sidebar{display:none}.app{min-height:100dvh}.main{min-width:0}</style>
</head><body><div class="app"><aside class="sidebar"></aside><main class="main"><div id="feed" class="feed"></div></main></div>
<div id="modal-root"></div><div id="toast-root"></div>
<script>
document.body.classList.add('concord-view');
const $=(s,r)=>(r||document).querySelector(s), $$=(s,r)=>[...(r||document).querySelectorAll(s)];
const enc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const room={name:'PosterChan Community',icon:'#',description:'history to read back through',
  channels:[{name:'general',private:false,id:'general-id'}],local:true,naddr:'naddr-scroll-check'};
localStorage.setItem('pc.concord.invites',JSON.stringify([room]));
localStorage.setItem('pc.concord.active','0');
// Enough history that a fling has somewhere to go, and varied lengths so rows are not uniform.
const body=i=>'Message '+i+' '+'word '.repeat(6+(i%11));
window.__fixture=Array.from({length:140},(_,i)=>({id:'message-'+i,pubkey:(i%9===0?'a':'b').repeat(64),
  by:i%9===0?'Me':'Alexandria Long Username',at:i+1,text:body(i),tags:[],reactions:{}}));
localStorage.setItem('pc.concord.test.'+room.naddr,JSON.stringify(window.__fixture));
window.__PC={$, $$, enc, niceNip05:s=>s, isView:v=>v==='concord',
 viewer:()=>({pubkey:'a'.repeat(64),npub:'npub1scroll',profile:{name:'Me',picture:''}}),
 profOf:pk=>({name:pk[0]==='a'?'Me':'Alexandria Long Username',picture:''}), LOGO:'',
 toast:()=>{}, relaySubscribe:()=>({close(){}}), relayQuery:async()=>[], relayQueryFrom:async()=>[],
 relayUrls:()=>[], publish:async()=>({ev:{}}), relayPublish:async()=>({ok:true}),relayPublishTo:async()=>1,
 signTemplate:async x=>x, linkify:s=>enc(s),linkCardHtml:()=>'',hydrateLinkCards:()=>{},
 osNotify:()=>{},askOsNotify:async()=> 'granted',copyValue:()=>{},startGroupCall:()=>{},
 uploadBlob:async()=>'',openEmojiPopover:()=>{},insertAt:()=>{},blossomPicker:null,modal:null,
 uiConfirm:async()=>true};
</script><script src="/static/js/client/concord.js"></script>
<script>PCConcord.render();window.__ready=true;</script></body></html>"""

# A scroller's position, plus everything needed to say WHERE it is in human terms.
PROBE = r"""(()=>{const b=document.querySelector('.cc-messages');if(!b)return null;
 const max=Math.max(0,b.scrollHeight-b.clientHeight);
 const rows=[...b.querySelectorAll('.cc-message[data-message-id]')];
 const first=rows.find(el=>el.getBoundingClientRect().bottom>b.getBoundingClientRect().top);
 return {top:b.scrollTop,max,height:b.scrollHeight,client:b.clientHeight,rows:rows.length,
         atTop:b.scrollTop<=1,atBottom:max-b.scrollTop<=1,
         firstVisible:first?first.dataset.messageId:null};})()"""


async def rpc(ws, method, params=None, ident=[0]):
    ident[0] += 1
    mine = ident[0]
    await ws.send(json.dumps({"id": mine, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("id") == mine:
            if "error" in msg:
                raise RuntimeError(msg["error"])
            return msg.get("result", {})


async def evaluate(ws, expression):
    result = await rpc(ws, "Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                                "returnByValue": True})
    value = result.get("result", {})
    if result.get("exceptionDetails"):
        raise RuntimeError(value.get("description") or result["exceptionDetails"])
    return value.get("value")


async def fling_back(ws, pixels=300, steps=14):
    """A REAL touch drag towards older messages — touchstart, a stream of touchmoves, touchend —
    dispatched through the browser's own input pipeline, so it scrolls with momentum and fires the
    very `touchstart`/`touchmove` that `scrollGesture()` uses to decide the hand is on the scroller.

    The whole family of bugs here is about a write landing while a gesture is alive, so a test that
    moves the scroller by assigning to `scrollTop` is testing the one thing that never happens on a
    phone. (`Input.synthesizeScrollGesture` with a touch source is a no-op under headless Chrome —
    it reports success and moves nothing, which would make this check vacuous rather than failing.)
    """
    x, y = WIDTH // 2, HEIGHT // 3
    await rpc(ws, "Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for i in range(1, steps + 1):
        await rpc(ws, "Input.dispatchTouchEvent",
                  {"type": "touchMove", "touchPoints": [{"x": x, "y": y + int(pixels * i / steps)}]})
        await asyncio.sleep(.012)
    await rpc(ws, "Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


async def watch(ws, seconds=2.4, every=0.12):
    """Sample the position rather than checking it once: a jerk is a change over time, and a single
    read after a fixed sleep is exactly how every earlier version of this passed while broken."""
    samples = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        samples.append(await evaluate(ws, PROBE))
        await asyncio.sleep(every)
    return samples


def verdict(name, settled, samples, problems):
    """DID THE READER KEEP THEIR PLACE IN THE CONVERSATION?

    The measure is WHICH MESSAGE is at the top of the viewport, never the pixel. When a picture
    above the reader finishes decrypting, holding the pixel is the BUG — the content grew, so the
    scroller must move by exactly that much to keep the same message in view. An earlier draft of
    this check asserted the pixel and called that legitimate correction a failure, which is the
    same class of mistake as the code it is testing.

    Samples, not one reading: a flash to the top that corrects itself is still a jerk, and checking
    once after a fixed sleep is how every previous version of this passed while broken.
    """
    if not samples or settled is None:
        problems.append(f"{name}: the chat scroller vanished mid-scenario")
        return
    want = settled["firstVisible"]
    # The first couple of samples cover the repaint itself; a position that is momentarily
    # unreadable mid-render is not a jerk. Everything after it is what the reader sees.
    watched = samples[2:] or samples[-1:]
    moved = [s for s in watched if s["firstVisible"] != want]
    if not moved:
        return
    ended = samples[-1]
    where = ("the TOP of the history (the oldest message)" if ended["atTop"] else
             "the BOTTOM (the newest message)" if ended["atBottom"] else
             f"message {ended['firstVisible']}")
    problems.append(
        f"{name}: the reader was reading {want} and was moved to {where} "
        f"({len(moved)}/{len(watched)} samples off). "
        f"Pixels: {settled['top']:.0f} -> {[round(s['top']) for s in samples]} "
        f"(range 0..{ended['max']:.0f})")


async def scenario(ws, url, name, disturb):
    """Enter the room, read backwards, then let `disturb` happen and watch."""
    problems = []
    await rpc(ws, "Page.navigate", {"url": url})
    for _ in range(120):
        if await evaluate(ws, "!!window.__ready && !!document.querySelector('.cc-channel')"):
            break
        await asyncio.sleep(.05)
    else:
        return [f"{name}: the room list never painted"]
    # Open the room the way a finger does — activateJoinedRoom is what pins to the bottom, and
    # entering by hand instead would skip the entry burst this test exists to measure.
    await evaluate(ws, "document.querySelector('.cc-channel').click()")
    for _ in range(120):
        probe = await evaluate(ws, PROBE)
        if probe and probe["client"] > 0 and probe["rows"] > 0:
            break
        await asyncio.sleep(.05)
    else:
        return [f"{name}: the chat pane never opened on the phone viewport"]
    # Let the entry burst finish pinning to the bottom before the reader touches anything.
    await asyncio.sleep(1.9)
    entry = await evaluate(ws, PROBE)
    if entry is None:
        return [f"{name}: no chat scroller after entry"]
    if not entry["atBottom"]:
        problems.append(f"{name}: entering the room did not land on the newest message: {entry}")
    await fling_back(ws)
    await asyncio.sleep(.45)            # let the fling's momentum finish on its own
    settled = await evaluate(ws, PROBE)
    if settled["atBottom"]:
        return problems + [f"{name}: the fling did not move the reader off the newest message: {settled}"]
    await disturb(ws)
    verdict(name, settled, await watch(ws), problems)
    return problems


async def drive(url):
    import websockets
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG}/json/list", timeout=3))
    page = next(t for t in tabs if t["type"] == "page")
    problems = []
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
        await rpc(ws, "Runtime.enable")
        await rpc(ws, "Emulation.setDeviceMetricsOverride", {
            "width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 2, "mobile": True,
            "screenWidth": WIDTH, "screenHeight": HEIGHT})
        await rpc(ws, "Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})

        async def nothing(_ws):
            return

        async def hydration_finishes(w):
            # activateJoinedRoom pins on render() and pins AGAIN in its `finally`, once the room's
            # history has loaded. On a real room that second pin lands seconds later — by which time
            # the reader is already somewhere they chose.
            await evaluate(w, "PCConcord.__testEnterChatBottom()")

        async def a_message_arrives(w):
            await evaluate(w, r"""(()=>{const k='pc.concord.test.naddr-scroll-check';
              const all=JSON.parse(localStorage.getItem(k));
              all.push({id:'message-new',pubkey:'b'.repeat(64),by:'Alexandria Long Username',
                        at:all.length+1,text:'a new message arriving while you read',tags:[],reactions:{}});
              localStorage.setItem(k,JSON.stringify(all));PCConcord.__testLiveRepaint();})()""")

        async def a_picture_decrypts(w):
            # Content gains height ABOVE the reader, which is what a late attachment does.
            await evaluate(w, r"""(()=>{const row=document.querySelector('.cc-message');
              if(!row)return;const d=document.createElement('div');
              d.style.cssText='height:260px;background:#333';row.appendChild(d);})()""")

        for name, disturb in (("settle", nothing),
                              ("hydrate", hydration_finishes),
                              ("message", a_message_arrives),
                              ("growth", a_picture_decrypts)):
            problems += await scenario(ws, url, name, disturb)
    return problems


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] == "/__concord_scroll.html":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        return super().do_GET()

    def log_message(self, *_args):
        pass


def main():
    chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
              or shutil.which("google-chrome"))
    if not chrome:
        print("SKIP no Chrome")
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", PORT), lambda *a, **k: Handler(*a, directory=ROOT, **k))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-concord-scroll-check"
    shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                             f"--remote-debugging-port={DEBUG}", f"--user-data-dir={profile}",
                             "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{DEBUG}/json/list", timeout=.5)
                break
            except Exception:
                time.sleep(.1)
        problems = asyncio.run(drive(f"http://127.0.0.1:{PORT}/__concord_scroll.html"))
        if problems:
            print("FAIL " + "\nFAIL ".join(problems))
            return 1
        print("OK  a phone reader keeps their place in room history through entry, hydration, "
              "an arriving message and late-growing content")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
