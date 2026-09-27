"""Every screen changed on 2026-09-27 fits a phone and a desktop -- measured, not eyeballed.

Run: venv-unified/bin/python -m pytest tests/client/test_new_ui_layouts_full_app.py
     PC_UI_SHOTS=/some/dir  … also writes a screenshot of each screen at each width.

The screens: the Git page's Private badge, Go Live's new Description/Tags/Language/Content-warning
fields, a viewer's stream page (language + tags + content-warning veil), the Streams card's language
badge, the cover picker's "server can't list" message, and the issue form's "not confirmed yet"
message. Each is rendered by the SHIPPED client in the plain web layout (the effects harness: fixture
relay + fetch) at 390px and 1280px, and must satisfy what a person would notice first:

  no-sideways   the page never scrolls horizontally (a long word or a fixed width pushing it out);
  on-screen     the thing that changed is visible and inside the viewport, not clipped off an edge;
  no-spill      its text fits its own box (a badge or a message that overflows reads as broken);
  tap-target    its controls are at least 28px tall -- a squeezed button is a missed tap on a phone.
"""
import asyncio
import base64
import json
import os
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client.test_effects_full_app import Browser, Handler, INIT

SHOTS = os.environ.get("PC_UI_SHOTS", "")

EXTRA = r"""
const __fx=window.fetch;
window.__lists={external:'missing', builtin:'missing'};
window.fetch=async function(url,opts={}){
  const u=String(url);
  if(u.includes('/api/instance-welcome/access')) return new Response(JSON.stringify({qualified:true,pubkey:window.__PC&&__PC.me()&&__PC.me().pubkey}),{status:200,headers:{'Content-Type':'application/json'}});
  if(u.includes('/api/streams/ingest')) return new Response(JSON.stringify({enabled:true,rtmp_url:'rtmp://fixture.invalid/live',
    stream_key:'k3y',token:'tok123',hls_url:'https://fixture.invalid/hls/tok123/index.m3u8'}),{status:200,headers:{'Content-Type':'application/json'}});
  if(/\/list\/[0-9a-f]{64}$/.test(u)) return new Response('404 Not Found',{status:404});
  return __fx(url,opts);
};
"""

# What each screen is judged on: (selector of the thing that changed, controls that must be tappable)
PROBE = r"""(function(sel, taps, wideIn){
  const vw=document.documentElement.clientWidth, out={sideways:document.documentElement.scrollWidth>vw+1, problems:[]};
  const els=[...document.querySelectorAll(sel)].filter(e=>e.offsetParent!==null || getComputedStyle(e).position==='fixed');
  if(!els.length) out.problems.push('nothing matched '+sel);
  for(const e of els.slice(0,6)){
    const r=e.getBoundingClientRect(), name=sel+' "'+(e.textContent||'').trim().slice(0,30)+'"';
    if(r.width<1||r.height<1) out.problems.push(name+' has no size');
    if(r.left<-1||r.right>vw+1) out.problems.push(name+' is off-screen ('+Math.round(r.left)+'..'+Math.round(r.right)+' of '+vw+')');
    if(e.scrollWidth>e.clientWidth+2 && getComputedStyle(e).overflowX!=='auto' && getComputedStyle(e).overflowX!=='scroll')
      out.problems.push(name+' spills out of its box ('+e.scrollWidth+'>'+e.clientWidth+')');
  }
  // A sentence poured into a narrow column is unreadable even when nothing overflows: a message must
  // use most of the sheet it sits in (the cover picker put its message in one 104px grid cell).
  if(wideIn) for(const e of els){ const box=e.closest(wideIn); if(!box) continue;
    const r=e.getBoundingClientRect(), b=box.getBoundingClientRect();
    if(r.width < b.width*0.6) out.problems.push(sel+' is squeezed into '+Math.round(r.width)+'px of a '+Math.round(b.width)+'px sheet'); }
  // Tap targets are a PHONE rule: on a desktop the app's small buttons are 21-23px by design.
  if(vw<600) for(const t of document.querySelectorAll(taps)){
    if(t.offsetParent===null) continue;
    const r=t.getBoundingClientRect();
    if(r.height<28) out.problems.push('control "'+(t.textContent||t.id||'').trim().slice(0,24)+'" is only '+Math.round(r.height)+'px tall');
    if(r.right>vw+1) out.problems.push('control "'+(t.textContent||t.id||'').trim().slice(0,24)+'" is off-screen');
  }
  return out;
})"""


async def _shot(b, name):
    if not SHOTS:
        return
    Path(SHOTS).mkdir(parents=True, exist_ok=True)
    data = (await b.call("Page.captureScreenshot", {"format": "png"}))["data"]
    Path(SHOTS, name + ".png").write_bytes(base64.b64decode(data))


async def _judge(b, name, sel, taps="none-at-all", wide_in=""):
    await asyncio.sleep(0.4)
    await _shot(b, name)
    r = await b.js(PROBE + "(" + json.dumps(sel) + "," + json.dumps(taps) + "," + json.dumps(wide_in) + ")")
    problems = (["the page scrolls sideways"] if r["sideways"] else []) + r["problems"]
    return [f"{name}: {p}" for p in problems]


async def run(width):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    problems = []
    with tempfile.TemporaryDirectory(prefix="pc-ui-layouts-") as profile:
        proc = subprocess.Popen(["/opt/google/chrome/chrome", "--headless=new", "--no-sandbox", "--disable-gpu",
                                 "--window-size=1440,1000", "--remote-debugging-port=0", "--user-data-dir=" + profile,
                                 "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                if Path(profile, "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(.1)
            port = Path(profile, "DevToolsActivePort").read_text().splitlines()[0]
            async with httpx.AsyncClient() as h:
                pages = (await h.get("http://127.0.0.1:" + port + "/json")).json()
            page = next(p for p in pages if p.get("type") == "page")
            async with websockets.connect(page["webSocketDebuggerUrl"], max_size=40_000_000) as ws:
                b = Browser(ws)
                await b.call("Page.enable")
                await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]}) if False else None
                await b.call("Emulation.setDeviceMetricsOverride",
                             {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 600})
                await b.call("Page.addScriptToEvaluateOnNewDocument", {"source": "window.__hasChats=false;" + INIT + EXTRA})
                await b.call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/client"})
                await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
                await b.until("document.body.classList.contains('guest')")
                # One key; its own PRIVATE repo, and somebody else's live stream with every new detail.
                await b.js(r"""(()=>{const key=new Uint8Array(32).fill(1), other=new Uint8Array(32).fill(7);
                  const now=Math.floor(Date.now()/1000);
                  window.__repo=NostrTools.finalizeEvent({kind:30617,created_at:now,content:'',tags:[['d','configs-with-a-rather-long-name'],
                    ['name','configs-with-a-rather-long-name'],['description','Private configuration for every machine'],['private','true'],
                    ['clone','https://fixture.invalid/git/npub1x/configs.git']]},key);
                  window.__stream=NostrTools.finalizeEvent({kind:30311,created_at:now,content:'',tags:[['d','s1'],['title','Drawing anime live with requests'],
                    ['streaming','https://fixture.invalid/hls/s1/index.m3u8'],['status','live'],['starts',String(now)],
                    ['summary','Sketching tonight — requests open. Schedule: every Friday. Links in my profile.'],
                    ['t','anime'],['t','drawing'],['t','requests'],['t','illustration'],['t','portuguese'],
                    ['L','ISO-639-1'],['l','pt','ISO-639-1'],['content-warning','flashing lights and loud music']]},other);
                  window.__events=[__repo, __stream];
                  document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);
                  document.querySelector('#btn-nsec-login').click()})()""")
                await b.until("!!__PC.me()")
                w = f"{width}px"

                # 1. Git page -- the Private badge beside a long repo name.
                await b.js("__PC.switchView('repos')")
                await b.until("!!document.querySelector('.repo-private')")
                problems += await _judge(b, f"git-private-{w}", ".repo-private")
                problems += await _judge(b, f"git-card-{w}", ".repo-card-hd")

                # 2. Go Live -- the new fields.
                await b.js("document.getElementById('nav-golive').click()")
                await b.until("!!document.querySelector('#gl-summary')")
                await b.js("(()=>{const c=document.querySelector('#gl-cw');c.checked=true;c.dispatchEvent(new Event('change'))})()")
                await b.js("document.querySelector('#gl-summary').scrollIntoView({block:'center'})")
                problems += await _judge(b, f"golive-fields-{w}", "#gl-summary, #gl-tags, #gl-lang, #gl-cwr, .gl-cwopt",
                                         "#gl-lang, .gl-cwopt")
                # The Description is a few lines, not the 220px writing pad every sheet textarea gets.
                h = await b.js("document.querySelector('#gl-summary').getBoundingClientRect().height")
                if h > 130:
                    problems.append(f"golive-fields-{w}: the Description box is {round(h)}px tall")
                await b.js("document.querySelector('#gl-cancel').click()")

                # 3. Somebody else's stream: language, tags, description, the content-warning veil.
                await b.js("__PC.openStream(window.__stream)")
                await b.until("!!document.querySelector('.st-details')")
                problems += await _judge(b, f"stream-details-{w}", ".st-details, .st-lang, .st-tag, .stream-view .about")
                problems += await _judge(b, f"stream-cw-{w}", "#st-cw", "#st-cw-show")

                # 4. The Streams card's language badge.
                await b.js("__PC.switchView('streams')")
                await b.until("!!document.querySelector('.stream-lang')")
                problems += await _judge(b, f"stream-card-{w}", ".stream-lang, .stream-card .stream-title")

                # 5. The cover picker's "your server can't list files" message.
                await b.js("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),mediaServer:'https://blossom.jumble.social/',blossomEnabled:true}))")
                await b.js("window.__PC.pickDriveImage(()=>{})")
                await b.until("/list your files/.test((document.querySelector('.bp-modal #bp-grid')||{}).textContent||'')")
                problems += await _judge(b, f"cover-nolist-{w}", ".bp-modal #bp-grid > .muted", ".bp-modal #bp-x", ".bp-modal")
                await b.js("document.querySelector('.bp-modal #bp-x').click()")

                # 6. The issue form's "not confirmed yet" message.
                await b.js("Relay.publish=async()=>({ok:false,msg:'timeout'})")
                await b.js("window.__PC.newRepoIssue(window.__repo)")
                await b.until("!!document.querySelector('#ri-subj')")
                await b.js("""document.querySelector('#ri-subj').value='Cover picker broken';
                              document.querySelector('#ri-body').value='It says my connection is down.';
                              document.querySelector('#ri-pub').click()""")
                await b.until("/confirmed/.test(document.querySelector('#ri-status').textContent)")
                problems += await _judge(b, f"issue-timeout-{w}", "#ri-status", "#ri-pub")
                # ONE explanation: the form's own. A generic "timeout" toast under it says the opposite.
                toasts = await b.js("[...document.querySelectorAll('.toast')].map(t=>t.textContent.trim())")
                if any("timeout" in t for t in toasts):
                    problems.append(f"issue-timeout-{w}: a bare 'timeout' toast repeats the form's message: {toasts}")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            server.shutdown()
    return problems


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_every_changed_screen_fits(width):
    problems = asyncio.run(run(width))
    assert not problems, "\n".join(problems)
