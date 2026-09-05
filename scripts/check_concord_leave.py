#!/usr/bin/env python3
"""Leaving a Concord community must STICK — on the phone, on the desktop, and on the next device.

Reported twice, three weeks apart: "mobile has no way to leave concord communities", and then
"make sure you can leave communities! on laptop, i loaded Communities and it brought me back to
Soapbox which I left many times".

Both halves are driven here against the SHIPPED client in a real browser, because both were
invisible from the code:

  * the control. Leave sat behind `window.confirm`, which a WebView may suppress outright — the
    branch behind it then never runs and the button silently does nothing. This check installs a
    `window.confirm` that FAILS the run if anything calls it, and clicks the control at phone and
    desktop width.
  * the durability. Leaving publishes a tombstone into the account's kind-13302 membership vault,
    which every device reads. What no other device read was the LOCALSTORAGE ledger that was the
    only thing refusing to rebuild the room from the owner's own kind-1 invite announcement — which
    discovery replays on every reconnect. So the second half reloads the page as a fresh device
    (same account, same relay, empty local storage), syncs, replays that announcement, and asserts
    the account is still out.

Then it re-joins, to prove the tombstone does not become a wall.
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
PORT = int(os.environ.get("PC_CHECK_PORT") or 9502)
DEBUG = PORT + 1000
WIDTHS = ((390, 844), (1280, 900))

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="stylesheet" href="/static/css/client.css">
<link rel="stylesheet" href="/static/css/concord.css">
<style>html,body{width:100%;height:100%;margin:0}.sidebar{display:none}.app{min-height:100dvh}.main{min-width:0}</style>
</head><body><div class="app"><aside class="sidebar"></aside><main class="main"><div id="feed" class="feed feed-dm"></div></main></div>
<div id="modal-root"></div><div id="toast-root"></div>
<script>
const $=(s,r)=>(r||document).querySelector(s), $$=(s,r)=>[...(r||document).querySelectorAll(s)];
const enc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const OWNER='a'.repeat(64), COMMUNITY='c'.repeat(64), NADDR='naddr1soapboxcheck';
const INVITE='https://armada.buzz/invite/'+NADDR+'#s3cr3t';
const bundle={owner:OWNER,community_root:'d'.repeat(64),channels:[{id:'gen',name:'general'}],relays:['wss://relay.test']};
const room={url:INVITE,naddr:NADDR,communityId:COMMUNITY,name:'Soapbox',description:'',
  channels:[{name:'general',private:false}],local:false,cord:{bundle}};

/* A NATIVE DIALOG IS A FAILURE, NOT A PROMPT. In the desktop shell it opens a real OS window and
   leaves the renderer unfocusable; in the APK's WebView it can be suppressed, and a suppressed
   confirm answers false — so a button gated on one does nothing at all. */
window.__nativeDialog=0;
window.confirm=()=>{window.__nativeDialog++;return false;};
window.alert=()=>{window.__nativeDialog++;};
window.prompt=()=>{window.__nativeDialog++;return null;};

/* One fake relay for the whole account, persisted across reloads so a "second device" (a reload
   with empty Concord storage) reads the same membership vault a real one would. */
let RELAY=[]; try{RELAY=JSON.parse(localStorage.getItem('__pc_fake_relay')||'[]');}catch(_){}
const saveRelay=()=>localStorage.setItem('__pc_fake_relay',JSON.stringify(RELAY));
let seq=Number(localStorage.getItem('__pc_fake_seq')||0);
const matches=(f,ev)=>{
  if(f.kinds&&!f.kinds.includes(ev.kind))return false;
  if(f.authors&&!f.authors.includes(ev.pubkey))return false;
  if(f['#d']){const d=((ev.tags||[]).find(t=>t[0]==='d')||[])[1]||'';if(!f['#d'].includes(d))return false;}
  return true;
};
const relayQuery=filters=>RELAY.filter(ev=>(filters||[]).some(f=>matches(f,ev)));

window.PosterCordReader={inspectControl:()=>({name:'Soapbox',channels:[{id:'gen',name:'general',private:false}]}),
  createBanWrap:null};
window.PosterCord={openInvite:()=>{throw new Error('no network in this check');},
  inviteDetails:()=>({linkSigner:OWNER,bootstrapRelays:[]})};

window.__PC={$, $$, enc, niceNip05:s=>s, isView:v=>v==='concord', LOGO:'',
 viewer:()=>({pubkey:OWNER,npub:'npub1owner',profile:{name:'Me',picture:''}}),
 profOf:pk=>({name:'Member',picture:''}),
 toast:m=>{(window.__toasts||(window.__toasts=[])).push(String(m));},
 uiConfirm:async()=>{window.__uiConfirms=(window.__uiConfirms||0)+1;return true;},
 nip44enc:async(_pk,plain)=>'E'+plain,
 nip44dec:async(_pk,ct)=>{if(typeof ct!=='string'||ct[0]!=='E')throw new Error('not ours');return ct.slice(1);},
 publish:async(kind,content,tags)=>{seq++;localStorage.setItem('__pc_fake_seq',String(seq));
   return {ev:{id:'ev'+seq,kind,pubkey:OWNER,created_at:1700000000+seq,tags:tags||[],content}};},
 relayPublishTo:async(_r,ev)=>{RELAY.push(ev);saveRelay();return 1;},
 relayPublish:async()=>({ok:true}),
 relayQuery:async f=>relayQuery(f),
 relayQueryFrom:async(_r,f)=>relayQuery(f),
 verifyRelayEvents:async e=>e,
 relaySubscribe:()=>'sub1', relayClose:()=>{}, relayUrls:()=>[],
 signTemplate:async x=>x, linkify:s=>enc(s), linkCardHtml:()=>'', hydrateLinkCards:()=>{},
 osNotify:()=>{}, askOsNotify:async()=>'granted', copyValue:()=>{}, startGroupCall:()=>{},
 uploadBlob:async()=>'', openEmojiPopover:()=>{}, insertAt:()=>{}, blossomPicker:null, modal:null};

window.__seedJoined=()=>{
  localStorage.setItem('pc.concord.invites',JSON.stringify([room]));
  localStorage.setItem('pc.concord.active','0');
};
window.__becomeFreshDevice=()=>{
  /* A second machine on the same account: same relay, nothing else. */
  localStorage.removeItem('pc.concord.invites');
  localStorage.removeItem('pc.concord.active');
  localStorage.removeItem('pc.concord.left.v1');
};
window.__announcement=()=>({url:INVITE,naddr:NADDR,secret:'s3cr3t',name:'Soapbox',
  description:'Soapbox',source:{pubkey:OWNER,created_at:1699999999,content:'join '+INVITE}});
window.__rooms=()=>{try{return JSON.parse(localStorage.getItem('pc.concord.invites')||'[]');}catch(_){return [];}};
window.__vault=async()=>{
  const out=[];
  for(const ev of RELAY.filter(e=>e.kind===13302||e.kind===33302)){
    try{out.push(JSON.parse(await window.__PC.nip44dec(OWNER,ev.content)));}catch(_){}
  }
  return out;
};
if(!sessionStorage.getItem('__pc_booted')){sessionStorage.setItem('__pc_booted','1');window.__seedJoined();}
</script><script src="/static/js/client/concord.js"></script>
<script>PCConcord.render();window.__ready=true;</script></body></html>"""


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] == "/__concord_leave.html":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
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


async def boot(ws, url, wait_for="!!window.__ready"):
    await rpc(ws, "Page.navigate", {"url": url})
    for _ in range(120):
        try:
            if await evaluate(ws, wait_for):
                return True
        except RuntimeError:
            pass
        await asyncio.sleep(.05)
    return False


async def drive(url):
    import websockets
    problems = []
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG}/json/list", timeout=3))
    page = next(t for t in tabs if t["type"] == "page")
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
        await rpc(ws, "Runtime.enable")
        await rpc(ws, "Page.enable")
        for width, height in WIDTHS:
            label = f"{width}px"
            await rpc(ws, "Emulation.setDeviceMetricsOverride",
                      {"width": width, "height": height, "deviceScaleFactor": 2,
                       "mobile": width < 800, "screenWidth": width, "screenHeight": height})
            # A clean account for each surface.
            await boot(ws, url)
            await evaluate(ws, "localStorage.clear();sessionStorage.clear()")
            if not await boot(ws, url, "!!window.__ready && !!document.querySelector('.cc-app')"):
                problems.append(f"{label} Concord never rendered")
                continue

            # --- 1. The control exists, is on screen, and is not a native dialog. -------------
            # MEASURED ON THE SCREEN THE APP OPENS ON. A phone starts on the rooms/channels pane
            # with `.cc-conversation` display:none, so a control that lives only in the
            # conversation header is present and 0x0 — which is what "mobile has no way to leave"
            # was. Both controls are checked wherever they are laid out.
            geometry = await evaluate(ws, r"""(()=>{const rect=id=>{const b=document.querySelector(id);
              if(!b)return null;const r=b.getBoundingClientRect(),cs=getComputedStyle(b);
              return {w:r.width,h:r.height,
                onScreen:r.width>0&&r.height>0&&r.left>=-0.5&&r.right<=innerWidth+0.5&&r.top>=-0.5&&r.bottom<=innerHeight+0.5,
                hidden:cs.display==='none'||cs.visibility==='hidden'};};
              return {pane:rect('#cc-leave-room'),chat:rect('#cc-leave-shortcut'),
                action:!!document.querySelector('#cc-leave-community')};})()""")
            if not geometry.get("action") or not (geometry.get("pane") or geometry.get("chat")):
                problems.append(f"{label} has no leave control: {geometry}")
                continue
            usable = [k for k in ("pane", "chat")
                      if geometry.get(k) and not geometry[k]["hidden"] and geometry[k]["onScreen"]
                      and geometry[k]["w"] >= 24 and geometry[k]["h"] >= 24]
            if not usable:
                problems.append(f"{label} no leave control is reachable on the opening screen: {geometry}")
                continue

            await evaluate(ws, "document.querySelector('#cc-leave-%s').click()"
                           % ("room" if "pane" in usable else "shortcut"))
            for _ in range(120):
                if not await evaluate(ws, "window.__rooms().length"):
                    break
                await asyncio.sleep(.05)
            after = await evaluate(ws, r"""(async()=>({rooms:window.__rooms().length,
              native:window.__nativeDialog,confirms:window.__uiConfirms||0,
              toasts:window.__toasts||[],vault:await window.__vault()}))()""")
            if after["native"]:
                problems.append(f"{label} leaving opened a NATIVE dialog ({after['native']} of them)")
            if not after["confirms"]:
                problems.append(f"{label} leaving asked for no confirmation at all")
            if after["rooms"]:
                problems.append(f"{label} the community survived the leave: {after['toasts']}")
            tombs = [t for doc in after["vault"] for t in (doc.get("tombstones") or [])]
            if not tombs:
                problems.append(f"{label} leaving published no tombstone to the membership vault")
            elif not any(t.get("invite_ref") or t.get("naddr") for t in tombs):
                problems.append(f"{label} the tombstone names no invite, so no announcement can be "
                                f"matched against it: {tombs}")
            if any(e.get("community_id") for doc in after["vault"] for e in (doc.get("entries") or [])
                   if e.get("community_id") == "c" * 64
                   and doc is after["vault"][-1]):
                problems.append(f"{label} the newest vault document still carries the left community")

            # --- 2. A FRESH DEVICE on the same account must stay out. ------------------------
            await evaluate(ws, "window.__becomeFreshDevice()")
            if not await boot(ws, url, "!!window.__ready && !!document.querySelector('.cc-app')"):
                problems.append(f"{label} the second device never rendered")
                continue
            await evaluate(ws, "PCConcord.syncArmadaMemberships(window.__PC,window.__PC.viewer())")
            await asyncio.sleep(.2)
            if await evaluate(ws, "window.__rooms().length"):
                problems.append(f"{label} the vault tombstone did not reach a fresh device")
            # Discovery replays the owner's OWN invite announcement. This is the resurrection.
            await evaluate(ws, "PCConcord.recoverOwnedInvite(window.__PC,window.__announcement())")
            await asyncio.sleep(.1)
            resurrected = await evaluate(ws, "window.__rooms()")
            if resurrected:
                problems.append(f"{label} the owner's own announcement re-joined a community the "
                                f"account had left: {[r.get('name') for r in resurrected]}")
            await evaluate(ws, "PCConcord.syncArmadaMemberships(window.__PC,window.__PC.viewer())")
            await asyncio.sleep(.2)
            if await evaluate(ws, "window.__rooms().length"):
                problems.append(f"{label} a membership pass left a resurrected community behind")

            # --- 3. Re-joining still works: a tombstone is not a wall. -----------------------
            rejoined = await evaluate(ws, r"""(async()=>{
              const room=JSON.parse(JSON.stringify(window.__rooms()));
              const fresh={url:'https://armada.buzz/invite/naddr1soapboxcheck#s3cr3t',
                naddr:'naddr1soapboxcheck',communityId:'c'.repeat(64),name:'Soapbox',description:'',
                channels:[{name:'general',private:false}],local:false,
                cord:{bundle:{owner:'a'.repeat(64),community_root:'d'.repeat(64),
                  channels:[{id:'gen',name:'general'}],relays:['wss://relay.test']}}};
              localStorage.setItem('pc.concord.invites',JSON.stringify([fresh]));
              const ok=await PCConcord.persistArmadaMembership(window.__PC,fresh);
              return {ok,left:PCConcord.leftCommunities('a'.repeat(64)).length};})()""")
            if not rejoined["ok"]:
                problems.append(f"{label} re-joining could not publish membership")
            if rejoined["left"]:
                problems.append(f"{label} re-joining left the community on the 'left' ledger")
            await evaluate(ws, "window.__becomeFreshDevice()")
            if not await boot(ws, url, "!!window.__ready && !!document.querySelector('.cc-app')"):
                problems.append(f"{label} the third device never rendered")
                continue
            await evaluate(ws, "PCConcord.syncArmadaMemberships(window.__PC,window.__PC.viewer())")
            for _ in range(60):
                if await evaluate(ws, "window.__rooms().length"):
                    break
                await asyncio.sleep(.05)
            if not await evaluate(ws, "window.__rooms().length"):
                problems.append(f"{label} a deliberate re-join was swallowed by the old tombstone")
    return problems


def main():
    chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
              or shutil.which("google-chrome"))
    if not chrome:
        print("SKIP no Chrome")
        return 2
    server = ThreadingHTTPServer(("127.0.0.1", PORT), lambda *a, **k: Handler(*a, directory=ROOT, **k))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = os.environ.get("PC_CHECK_PROFILE") or "/tmp/pc-concord-leave-check"
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
        problems = asyncio.run(drive(f"http://127.0.0.1:{PORT}/__concord_leave.html"))
        if problems:
            print("FAIL " + "\nFAIL ".join(problems))
            return 1
        print("OK  Concord leave works on phone and desktop and survives the next device")
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
