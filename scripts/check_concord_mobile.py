#!/usr/bin/env python3
"""Render the shipped Concord workspace at phone widths and exercise its OS-style drawer."""

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
PORT = int(os.environ.get("PC_CHECK_PORT") or 9498)
DEBUG = PORT + 1000
WIDTHS = ((390, 844), (360, 780))

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="stylesheet" href="/static/css/client.css">
<link rel="stylesheet" href="/static/css/concord.css">
<style>html,body{width:100%;height:100%;margin:0}.sidebar{display:none}.app{min-height:100dvh}.main{min-width:0}.mobilenav{height:61px}</style>
</head><body><div class="app"><aside class="sidebar"></aside><main class="main"><div id="feed" class="feed feed-dm"></div></main></div>
<nav class="mobilenav"><button>Home</button><button>Messages</button><button>Discover</button></nav>
<div id="modal-root"></div><div id="toast-root"></div>
<script>
const $=(s,r)=>(r||document).querySelector(s), $$=(s,r)=>[...(r||document).querySelectorAll(s)];
const enc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const room={name:'PosterChan Community',icon:'🕊',description:'A real community description that has to clamp cleanly.',
  channels:[{name:'general',private:false,id:'general-id'},{name:'support-and-long-room-name',private:false,id:'support-id'}],
  local:true,naddr:'naddr-mobile-layout'};
localStorage.setItem('pc.concord.invites',JSON.stringify([room]));
localStorage.setItem('pc.concord.active','0');
const fixtureMessages=Array.from({length:36},(_,i)=>({id:'message-'+i,pubkey:(i===35?'a':'b').repeat(64),by:i===35?'Me':'Alexandria Long Username',at:i+1,
 text:i===0?'A message with https://example.test/a/very/long/path/that/must/not/push/the/phone/sideways and enough words to wrap naturally.':'Message '+i+' with enough text to give the chat a real scroll range.',tags:[],reactions:i===0?{'👍':['b'.repeat(64)]}:{}}));
localStorage.setItem('pc.concord.test.'+room.naddr,JSON.stringify(fixtureMessages));
window.__PC={$, $$, enc, niceNip05:s=>s, isView:v=>v==='concord',
 viewer:()=>({pubkey:'a'.repeat(64),npub:'npub1mobile',profile:{name:'Me',picture:''}}),
 profOf:pk=>({name:pk[0]==='a'?'Me':'Alexandria Long Username',picture:''}), LOGO:'',
 toast:()=>{}, relaySubscribe:()=>({close(){}}), relayQuery:async()=>[], relayQueryFrom:async()=>[],
 relayUrls:()=>[], publish:async()=>({ev:{}}), relayPublish:async()=>({ok:true}),relayPublishTo:async()=>1,
 signTemplate:async x=>x, linkify:s=>enc(s),linkCardHtml:()=>'',hydrateLinkCards:()=>{},
 osNotify:()=>{},askOsNotify:async()=> 'granted',copyValue:()=>{},startGroupCall:()=>{},
 uploadBlob:async()=>'',openEmojiPopover:()=>{},insertAt:()=>{},blossomPicker:null,modal:null,
 uiConfirm:async()=>true};
</script><script src="/static/js/client/concord.js"></script>
<script>PCConcord.render();window.__ready=true;</script></body></html>"""


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] == "/__concord_mobile.html":
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


AUDIT = r"""(() => { const q=s=>document.querySelector(s),r=e=>{if(!e)return null;const x=e.getBoundingClientRect();return {left:x.left,top:x.top,right:x.right,bottom:x.bottom,width:x.width,height:x.height}},vis=e=>e&&getComputedStyle(e).display!=='none';
 const app=q('.cc-app'),rail=q('.cc-communities'),channels=q('.cc-channels'),chat=q('.cc-conversation'),msgs=q('.cc-messages'),compose=q('.cc-compose'),members=q('.cc-members-pane');
 return {pageOverflow:document.documentElement.scrollWidth-window.innerWidth,bodyOverflow:document.body.scrollWidth-window.innerWidth,
   app:r(app),rail:r(rail),channels:r(channels),chat:r(chat),messages:r(msgs),compose:r(compose),
   railVisible:vis(rail),channelsVisible:vis(channels),chatVisible:vis(chat),membersVisible:vis(members),
   appClass:app&&app.className,viewport:[innerWidth,innerHeight]}; })()"""


async def drive(url):
    import websockets
    problems = []
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG}/json/list", timeout=3))
    page = next(t for t in tabs if t["type"] == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
        await rpc(ws, "Runtime.enable")
        for width, height in WIDTHS:
            await rpc(ws, "Emulation.setDeviceMetricsOverride", {"width": width, "height": height,
                      "deviceScaleFactor": 2, "mobile": True, "screenWidth": width, "screenHeight": height})
            await rpc(ws, "Page.navigate", {"url": url})
            for _ in range(80):
                if await evaluate(ws, "!!window.__ready && !!document.querySelector('.cc-channel')"):
                    break
                await asyncio.sleep(.05)
            before = await evaluate(ws, AUDIT)
            if before["pageOverflow"] > 1 or before["bodyOverflow"] > 1:
                problems.append(f"{width}px room list overflows horizontally: {before}")
            if not before["railVisible"] or not before["channelsVisible"] or before["chatVisible"]:
                problems.append(f"{width}px does not show the OS rail + room pane first: {before}")
            if abs(before["rail"]["width"] - 58) > 2:
                problems.append(f"{width}px community rail is {before['rail']['width']:.1f}px, expected 58px")
            await evaluate(ws, "document.querySelector('.cc-channel').click()")
            await asyncio.sleep(.08)
            chat = await evaluate(ws, AUDIT)
            if chat["pageOverflow"] > 1 or chat["bodyOverflow"] > 1:
                problems.append(f"{width}px chat overflows horizontally: {chat}")
            if not chat["chatVisible"] or chat["railVisible"] or chat["channelsVisible"] or chat["membersVisible"]:
                problems.append(f"{width}px chat did not take the phone viewport cleanly: {chat}")
            if chat["chat"] and abs(chat["chat"]["width"] - width) > 2:
                problems.append(f"{width}px chat width is {chat['chat']['width']:.1f}px")
            deletion = await evaluate(ws, r"""(async()=>{const box=document.querySelector('.cc-messages');box.scrollTop=24;box.dispatchEvent(new Event('scroll'));const top=box.scrollTop,count=document.querySelectorAll('.cc-message').length,button=[...document.querySelectorAll('[data-cc-delete]')].at(-1);await button.onclick();await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));return {top,bottom:box.scrollTop,before:count,after:document.querySelectorAll('.cc-message').length,app:document.querySelector('.cc-app').className,overflow:document.documentElement.scrollWidth-innerWidth};})()""")
            if deletion["after"] != deletion["before"] - 1:
                problems.append(f"{width}px delete did not remove exactly one message: {deletion}")
            if abs(deletion["bottom"] - deletion["top"]) > 2:
                problems.append(f"{width}px deleting an off-screen message moved the chat: {deletion}")
            if deletion["overflow"] > 1 or "show-chat" not in deletion["app"]:
                problems.append(f"{width}px deletion damaged the chat layout: {deletion}")
            await evaluate(ws, "document.querySelector('#cc-back-channels').click()")
            await asyncio.sleep(.05)
            drawer = await evaluate(ws, AUDIT)
            if not drawer["railVisible"] or not drawer["channelsVisible"] or not drawer["chatVisible"]:
                problems.append(f"{width}px room drawer did not overlay the live chat: {drawer}")
            if drawer["channels"] and drawer["channels"]["right"] > width + 1:
                problems.append(f"{width}px drawer leaves the viewport: {drawer['channels']}")
            if not await evaluate(ws, "getComputedStyle(document.querySelector('#cc-drawer-backdrop')).display!=='none'"):
                problems.append(f"{width}px drawer has no dismissible backdrop")
            await evaluate(ws, "document.querySelector('#cc-drawer-backdrop').click()")
            if await evaluate(ws, "document.querySelector('.cc-app').classList.contains('drawer-open')"):
                problems.append(f"{width}px drawer did not close")
    return problems


def main():
    chrome = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("google-chrome")
    if not chrome:
        print("SKIP no Chrome")
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", PORT), lambda *a, **k: Handler(*a, directory=ROOT, **k))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Per-run, because the browser checks run CONCURRENTLY: two Chromes on one profile
    # directory corrupt it and one dies on a lock, intermittently — which reads as a flaky
    # feature rather than a flaky harness. The literal is the standalone-run default.
    profile = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-concord-mobile-check"
    shutil.rmtree(profile, ignore_errors=True)
    proc = subprocess.Popen([chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
                             f"--remote-debugging-port={DEBUG}", f"--user-data-dir={profile}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{DEBUG}/json/list", timeout=.5)
                break
            except Exception:
                time.sleep(.1)
        problems = asyncio.run(drive(f"http://127.0.0.1:{PORT}/__concord_mobile.html"))
        if problems:
            print("FAIL " + "\nFAIL ".join(problems))
            return 1
        print("OK  Concord mobile matches the OS rail/drawer model at 390px and 360px")
        return 0
    finally:
        proc.terminate()
        try: proc.wait(3)
        except subprocess.TimeoutExpired: proc.kill()
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
