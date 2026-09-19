"""Social survives a REAL offline -> online cycle without destroying an open reply or losing place.

BACKLOG_BETA3 §4: "Social refreshes after offline without destroying open replies/place." The other
parts of that line (desktop scrollbar, bounded article images, opens-at-top) already have tests; the
offline-refresh half was left open because it needs a genuine offline/online cycle with an open reply
against a real relay socket. This is that cycle, in a real headless Chrome driving the shipped client:

  1. log in, open the GLOBAL timeline; a fixture relay serves a set of signed kind-1 posts
  2. open a REPLY on a card and type an unsent draft into it (the modal lives in #modal-root)
  3. scroll the feed down and remember the card at the top
  4. go OFFLINE, drop the socket, come back ONLINE -> the client's own `online` handler runs
     `_tlForeground()`+`_resumeRelay()`, the socket reconnects, and `Relay.onReconnect` reconciles
  5. assert the reply is STILL open with its text, and the feed is still populated at the same place

Only the relay WebSocket and instance HTTP are fixtures; the client's DOM, timeline reconcile,
composer and reconnect logic all run unchanged. PC_SOCIAL_OFFLINE_APP_ROOT selects an isolated
implementation checkout.
"""
import asyncio
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer

import httpx
import pytest
import websockets

from tests.client import test_effects_full_app as full

APP_ROOT = Path(os.environ.get("PC_SOCIAL_OFFLINE_APP_ROOT", Path(__file__).resolve().parents[2]))

# A relay that SERVES a timeline (unlike the drafts fixture, which is silent), survives being dropped
# and reopened (Relay.wake / onclose reconnect), and answers every kind-1 REQ with the same posts so a
# reconnect reconciles rather than empties. Posts are signed in-page with NostrTools so they verify.
SOCKET = r'''
window.__posts=null; window.__dropCount=0; window.__reqCount=0;
function __buildPosts(){
 if(window.__posts) return window.__posts;
 const sk=new Uint8Array(32).fill(7);
 const now=Math.floor(Date.now()/1000);
 const out=[];
 for(let i=0;i<24;i++){
  const ev=NostrTools.finalizeEvent({kind:1,created_at:now-i*60,tags:[],content:'fixture timeline post number '+i+' — some text to give the card height'},sk);
  out.push(ev);
 }
 window.__posts=out; return out;
}
class FixtureSocket extends EventTarget{
 static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;__sockets.push(this);
  setTimeout(()=>{this.readyState=1;this.fire('open',{});},8);}
 fire(type,data){const e=type==='message'?new MessageEvent(type,{data:JSON.stringify(data)}):new Event(type);this['on'+type]?.(e);this.dispatchEvent(e);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;}if(!Array.isArray(m))return;
  if(m[0]==='REQ'){
   window.__reqCount++;
   const subid=m[1];
   const filters=m.slice(2);
   const wantsNotes=filters.some(f=>!f.kinds||f.kinds.includes(1));
   if(wantsNotes && (window.NostrTools)){
    const posts=__buildPosts();
    for(const ev of posts) this.fire('message',['EVENT',subid,ev]);
   }
   setTimeout(()=>this.fire('message',['EOSE',subid]),12);
  }
  if(m[0]==='EVENT'){ // our own reply publish, if it ever goes out — just ack it
   setTimeout(()=>this.fire('message',['OK',m[1].id,true,'fixture accepted']),12);
  }
 }
 close(){if(this.readyState===3)return;this.readyState=3;this.fire('close',{});}
}
window.WebSocket=FixtureSocket;
// Drop every live socket, the way a lost network would — Relay's onclose then reconnects.
window.__dropSockets=()=>{window.__dropCount++;__sockets.filter(s=>s.readyState===1).forEach(s=>s.close());};
window.__feedCards=()=>document.querySelectorAll('#feed article, #feed .note').length;
window.__replyOpen=()=>{const m=document.querySelector('#modal-root .modal #cmp');return m?m.value:null;};
window.__topCardKey=()=>{const f=document.querySelector('#feed');if(!f)return null;
 for(const a of f.querySelectorAll('article,.note')){const r=a.getBoundingClientRect();
  if(r.bottom>80){return a.getAttribute('data-key')||a.getAttribute('data-id')||a.id||'';}}
 return null;};
'''
INIT = full.INIT.split("class FixtureSocket")[0] + SOCKET


async def run_cycle():
    full.ROOT = APP_ROOT
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="pc-social-offline-") as profile:
        proc = subprocess.Popen(
            ["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
             "--window-size=1440,1000", "--remote-debugging-port=0", "--user-data-dir=" + profile,
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
                await b.call("Network.enable")
                await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
                await b.call("Emulation.setDeviceMetricsOverride",
                             {"width": 390, "height": 780, "deviceScaleFactor": 1, "mobile": True})
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": INIT})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                await b.js("(()=>{const key=new Uint8Array(32).fill(1);"
                           "document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);"
                           "document.querySelector('#btn-nsec-login').click()})()")
                await b.until('!!__PC.me() && Relay.status==="ok"')

                # The GLOBAL timeline shows every post the fixture serves, no follows needed.
                await b.js("__PC.switchView('global')")
                await b.until("window.__feedCards() >= 8")

                # Open a reply on the first card and type an unsent draft into it.
                await b.js("(()=>{const c=document.querySelector('#feed article,#feed .note');"
                           "const r=c.querySelector('[data-a=\"reply\"]');r.click();})()")
                await b.until("!!document.querySelector('#modal-root .modal #cmp')")
                await b.js("(()=>{const ta=document.querySelector('#modal-root .modal #cmp');"
                           "ta.value='UNSENT-REPLY-MARKER-42';ta.dispatchEvent(new Event('input',{bubbles:true}));})()")

                # Scroll the feed down and remember where we are.
                await b.js("(()=>{const f=document.querySelector('#feed');f.scrollTop=Math.min(600,f.scrollHeight);})()")
                await asyncio.sleep(.2)
                before = {
                    "reply": await b.js("window.__replyOpen()"),
                    "cards": await b.js("window.__feedCards()"),
                    "top": await b.js("window.__topCardKey()"),
                    "scroll": await b.js("document.querySelector('#feed').scrollTop"),
                }
                assert before["reply"] == "UNSENT-REPLY-MARKER-42", before
                assert before["cards"] >= 8, before

                # THE CYCLE: offline event, real socket drop, online event. The client's own
                # window 'online' handler (_tlForeground + _resumeRelay) and the socket reconnect
                # (Relay.onReconnect) run for real.
                await b.js("window.dispatchEvent(new Event('offline'))")
                await b.js("window.__dropSockets()")
                await asyncio.sleep(.3)
                await b.js("window.dispatchEvent(new Event('online'))")
                # give wake/reconnect/reconcile time to run; force a wake past the throttle too
                await b.js("try{Relay.wake&&Relay.wake()}catch(_){}" )
                await b.until('Relay.status==="ok" && window.__reqCount >= 2')
                await asyncio.sleep(.6)

                after = {
                    "reply": await b.js("window.__replyOpen()"),
                    "cards": await b.js("window.__feedCards()"),
                    "top": await b.js("window.__topCardKey()"),
                    "scroll": await b.js("document.querySelector('#feed').scrollTop"),
                    "dropped": await b.js("window.__dropCount"),
                    "reqs": await b.js("window.__reqCount"),
                }
                return before, after
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            server.shutdown()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="no chrome")
def test_offline_online_keeps_the_open_reply_and_the_place():
    before, after = asyncio.get_event_loop().run_until_complete(run_cycle())
    # The socket really dropped and the timeline really re-queried after coming back.
    assert after["dropped"] >= 1, after
    assert after["reqs"] >= 2, after
    # THE OPEN REPLY SURVIVES, with its unsent text intact — it lives in #modal-root, which the
    # reconnect reconcile of #feed must never touch.
    assert after["reply"] == "UNSENT-REPLY-MARKER-42", ("open reply was destroyed by the refresh", before, after)
    # THE PLACE SURVIVES: the feed is still populated (not blanked to a spinner) and the card that was
    # at the top is still the anchor — the visible-card anchor, not a naive scrollTop.
    assert after["cards"] >= 8, ("the timeline was blanked by the refresh", before, after)
    assert after["top"] == before["top"], ("lost the reading place across the refresh", before, after)
