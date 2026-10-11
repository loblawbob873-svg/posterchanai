"""The phone's top search bar gets out of the way while you read.

Reported by a member (mobile web timeline on poster.place): "The search bar is always taking up
space during scrolling ... it's actually eating up valuable reading space at all times."

The real client, in headless Chrome at 390px with mobile emulation, over a fixture relay that serves
enough signed posts to scroll:

  * at the top the bar is shown; scrolling the feed DOWN slides it off screen (a transform, so the
    card under the finger does not move) and the feed shows MORE content -- the visible reading height
    grows by about the bar's height; scrolling UP a little, or reaching the top, brings it back;
  * the `/` shortcut and a tap on the top edge bring it back AND focus it, without moving a card;
  * a focused input, or one holding text, is never hidden;
  * prefers-reduced-motion: the slide is instant;
  * Settings -> Timeline -> "Search bar: Always show" keeps it in place;
  * at 1280px nothing changes: the desktop's search lives in the sidebar and never moves.

And the pref FOLLOWS THE ACCOUNT (the persist-and-hydrate rule): set on device A -> published in
`pcai:client-prefs` -> a FRESH profile B restores it and the bar behaves accordingly; and a change made
while the relays cannot answer is kept on the device and put back up on its next start.
"""
import asyncio
import json
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import websockets

from tests.client import test_effects_full_app as full
from tests.client import test_social_offline_refresh_full_app as soc

CHROME = "/opt/google/chrome/chrome"

# Remember every published 30078 and answer a 30078 query from a seeded store -- or, while
# localStorage.__sbDark is set, not at all (no EVENT, no EOSE: the relays "could not be asked").
PREFS = r'''
window.__published=[];
(()=>{const Orig=window.WebSocket;
 const send=Orig.prototype.send;
 Orig.prototype.send=function(raw){let m;try{m=JSON.parse(raw);}catch(_){return send.call(this,raw);}
   if(Array.isArray(m)&&m[0]==='EVENT'&&m[1]&&m[1].kind===30078){window.__published.push(m[1]);}
   if(Array.isArray(m)&&m[0]==='REQ'&&m.slice(2).some(f=>(f.kinds||[]).includes(30078))){
     let dark=false;try{dark=localStorage.getItem('__sbDark')==='1';}catch(_){}
     if(dark)return;
     const want=m.slice(2).filter(f=>(f.kinds||[]).includes(30078));
     const seed=(window.__seedPrefs||[]).filter(ev=>want.some(f=>(!f['#d']||f['#d'].includes((ev.tags.find(t=>t[0]==='d')||[])[1]))));
     setTimeout(()=>{for(const ev of seed)this.fire('message',['EVENT',m[1],ev]);this.fire('message',['EOSE',m[1]]);},10);
     return;}
   return send.call(this,raw);};})();
window.__sbScroll=async(dy,steps=8)=>{const f=document.querySelector('#feed');
  for(let i=0;i<steps;i++){f.scrollTop+=dy/steps;
    await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));}
  await new Promise(r=>setTimeout(r,450));};
window.__sbState=()=>{const tb=document.querySelector('.main>.topbar'),f=document.querySelector('#feed');
  const cs=getComputedStyle(tb),r=tb.getBoundingClientRect(),fr=f.getBoundingClientRect();
  const shown=cs.display!=='none'&&cs.visibility!=='hidden'&&r.bottom>1;
  const nav=document.querySelector('.mobilenav');
  const navTop=nav&&getComputedStyle(nav).display!=='none'?nav.getBoundingClientRect().top:innerHeight;
  let cover=Math.max(fr.top,shown?r.bottom:0);
  const tabs=f.querySelector('.tl-tabs');const tabsTop=tabs?Math.round(tabs.getBoundingClientRect().top):null;
  if(tabs){const t=tabs.getBoundingClientRect();if(t.top<=cover+1)cover=Math.max(cover,t.bottom);}
  const m=cs.transform&&cs.transform!=='none'?new DOMMatrixReadOnly(cs.transform):null;
  const cards=[...f.querySelectorAll('article.note,.note')].slice(0,30).map(a=>Math.round(a.getBoundingClientRect().top));
  const inp=document.getElementById('search-input');
  return {shown,top:r.top,bottom:r.bottom,height:r.height,ty:m?m.m42:0,transition:cs.transitionDuration,
          visible:Math.round(Math.min(fr.bottom,navTop,innerHeight)-Math.max(cover,0)),scroll:f.scrollTop,cards,
          focused:document.activeElement===inp,mainScroll:document.querySelector('.main').scrollTop,
          sbAuto:document.body.classList.contains('sb-auto'),tabsTop};};
'''


async def device(act, *, width=390, height=780, mobile=True, seed=(), reduced=False):
    full.ROOT = Path(__file__).resolve().parents[2]
    server = ThreadingHTTPServer(("127.0.0.1", 0), full.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tmp = tempfile.TemporaryDirectory(prefix="pc-sbhide-", ignore_cleanup_errors=True)
    prof = tmp.name
    p = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
                          "--remote-debugging-port=0", "--user-data-dir=" + prof, "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        port_file = Path(prof, "DevToolsActivePort")
        for _ in range(200):
            if port_file.exists() and port_file.read_text().strip():
                break
            await asyncio.sleep(.1)
        port = port_file.read_text().splitlines()[0]
        async with httpx.AsyncClient() as h:
            for _ in range(100):
                try:
                    pages = (await h.get(f"http://127.0.0.1:{port}/json")).json()
                    if any(x["type"] == "page" for x in pages):
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(.1)
        async with websockets.connect(next(x for x in pages if x["type"] == "page")["webSocketDebuggerUrl"],
                                      max_size=20_000_000) as ws:
            b = full.Browser(ws)
            await b.call("Page.enable")
            # A real phone's page has focus; headless Chrome's does not, and without it no `focus` event fires.
            await b.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
            await b.call("Network.setBlockedURLs", {"urls": ["https://*", "wss://*"]})
            await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=height, deviceScaleFactor=1,
                                                                    mobile=mobile, screenWidth=width,
                                                                    screenHeight=height))
            if reduced:
                await b.call("Emulation.setEmulatedMedia",
                             {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
            await b.call("Page.addScriptToEvaluateOnNewDocument",
                         {"source": soc.INIT + PREFS + "window.__seedPrefs=" + json.dumps(list(seed)) + ";"})
            b.base = f"http://127.0.0.1:{server.server_port}/client"
            await b.call("Page.navigate", {"url": b.base})
            await boot(b)
            return await act(b)
    finally:
        p.terminate()
        p.wait(timeout=10)
        server.shutdown()
        tmp.cleanup()


async def boot(b):
    await b.until('!!window.__PC && !!window.NostrTools && document.readyState==="complete"')
    await b.until("document.body.classList.contains('guest') || !!__PC.me()")
    if await b.js("!__PC.me()"):
        await b.js("(()=>{const key=new Uint8Array(32).fill(1);document.querySelector('#nsec-input').value="
                   "NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()")
    await b.until('!!__PC.me() && Relay.status==="ok"')
    await b.js("__PC.switchView('global')")
    await b.until("window.__feedCards() >= 12")
    await asyncio.sleep(2.5)            # the account's prefs are restored after sign-in


async def state(b):
    return await b.js("window.__sbState()")


async def to_top(b):
    await b.js("(async()=>{const f=document.querySelector('#feed');f.scrollTop=0;"
               "await new Promise(r=>setTimeout(r,450));})()")


async def open_timeline_settings(b):
    await b.js("__PC.switchView('settings')")
    await b.until("!!document.querySelector('.us-tab[data-tab=\"timeline\"]')")
    await b.js("document.querySelector('.us-tab[data-tab=\"timeline\"]').click()")
    await b.until("!!document.getElementById('set-search-bar')")


async def choose(b, value):
    await open_timeline_settings(b)
    await b.js("(()=>{const s=document.getElementById('set-search-bar');s.value=%s;"
               "s.dispatchEvent(new Event('change'));})()" % json.dumps(value))
    await b.js("__PC.switchView('global')")
    await b.until("window.__feedCards() >= 12")
    await to_top(b)


def _prefs_of(published):
    docs = [e for e in published if ["d", "pcai:client-prefs"] in [t[:2] for t in e.get("tags", [])]]
    return json.loads(docs[-1]["content"]) if docs else None


needs_chrome = pytest.mark.skipif(not Path(CHROME).exists(), reason="Chrome required")


@needs_chrome
def test_scrolling_down_slides_the_bar_away_and_gives_the_reading_space_back():
    async def act(b):
        out = {"top": await state(b)}
        await b.js("window.__sbScroll(900)")
        out["down"] = await state(b)
        # The `/` shortcut: shown again, focused, and NOT ONE CARD MOVED (it is an overlay, not a reflow).
        await b.js("document.activeElement&&document.activeElement.blur&&document.activeElement.blur()")
        await b.js("document.body.dispatchEvent(new KeyboardEvent('keydown',{key:'/',bubbles:true,cancelable:true}))")
        await asyncio.sleep(.5)
        out["slash"] = await state(b)
        # A focused input is never hidden, however far you scroll.
        await b.js("window.__sbScroll(600)")
        out["focused_scroll"] = await state(b)
        # Text in the box keeps it too, after the focus is gone.
        await b.js("(()=>{const i=document.getElementById('search-input');i.value='cats';i.blur();})()")
        await b.js("window.__sbScroll(600)")
        out["text_scroll"] = await state(b)
        await b.js("document.getElementById('search-input').value=''")
        await b.js("window.__sbScroll(600)")
        out["down2"] = await state(b)
        # Scrolling UP a little brings it back.
        await b.js("window.__sbScroll(-90, 4)")
        out["up"] = await state(b)
        # ...down again, then a tap on the top edge: shown and focused.
        await b.js("window.__sbScroll(600)")
        out["down3"] = await state(b)
        await b.js("(()=>{const el=document.elementFromPoint(innerWidth/2,3);el&&el.click();})()")
        await asyncio.sleep(.5)
        out["tap"] = await state(b)
        await b.js("document.activeElement&&document.activeElement.blur&&document.activeElement.blur()")
        await b.js("window.__sbScroll(600)")
        await to_top(b)
        out["back_top"] = await state(b)
        return out
    s = asyncio.run(device(act))
    top, down = s["top"], s["down"]
    assert top["shown"] and top["height"] > 30, ("the bar is not shown at the top", top)
    assert not down["shown"] and down["bottom"] <= 0 and down["ty"] < 0, (
        "scrolling the feed down left the search bar on screen", down)
    # The stuck timeline tabs ride up with it: nothing peeks out above them.
    assert down["tabsTop"] is not None and down["tabsTop"] <= 2, ("a strip of feed shows above the stuck tabs", down)
    # Mid-feed, hidden vs shown (the `up` state below): the reading area grows by about the bar's height.
    assert down["visible"] - s["up"]["visible"] >= 0.8 * top["height"], (
        "hiding the bar did not give the reading space back", s["up"]["visible"], down["visible"], top["height"])
    slash = s["slash"]
    assert slash["shown"] and slash["focused"], ("`/` did not bring the bar back focused", slash)
    assert slash["cards"] == down["cards"] and slash["mainScroll"] == 0, (
        "bringing the bar back moved the posts under the reader", down["cards"], slash["cards"], slash["mainScroll"])
    assert s["focused_scroll"]["shown"], ("a focused search box was hidden", s["focused_scroll"])
    assert s["text_scroll"]["shown"], ("a search box holding text was hidden", s["text_scroll"])
    assert not s["down2"]["shown"], s["down2"]
    assert s["up"]["shown"], ("scrolling up a little did not bring the bar back", s["up"])
    assert abs(s["up"]["tabsTop"] - s["up"]["bottom"]) <= 10, (
        "the stuck tabs are not tucked under the returning bar", s["up"]["tabsTop"], s["up"]["bottom"])
    assert not s["down3"]["shown"], s["down3"]
    assert s["tap"]["shown"] and s["tap"]["focused"], ("a tap on the top edge did not bring the bar back", s["tap"])
    assert s["back_top"]["shown"], ("the bar is not back at the top of the feed", s["back_top"])


@needs_chrome
def test_reduced_motion_hides_and_shows_instantly():
    async def act(b):
        await b.js("window.__sbScroll(900)")
        return await state(b)
    down = asyncio.run(device(act, reduced=True))
    assert not down["shown"], ("the bar did not hide under reduced motion", down)
    assert all(float(x.rstrip("s") or 0) == 0 for x in down["transition"].split(",")), (
        "prefers-reduced-motion still animates the bar", down["transition"])


@needs_chrome
def test_always_show_keeps_the_bar_and_the_desktop_never_changes():
    async def phone(b):
        await choose(b, "show")
        await b.js("window.__sbScroll(900)")
        return await state(b), await b.js("ClientSettings.get('searchBar')")
    down, stored = asyncio.run(device(phone))
    assert stored == "show", stored
    assert down["shown"] and down["top"] >= -1, ("'Always show' still hid the bar", down)

    async def desktop(b):
        q = ("(()=>{const r=document.getElementById('nav-search-input').getBoundingClientRect();"
             "return [Math.round(r.top),Math.round(r.height)];})()")
        before = await b.js(q)
        await b.js("window.__sbScroll(900)")
        return before, await b.js(q), await state(b)
    before, after, st = asyncio.run(device(desktop, width=1280, height=800, mobile=False))
    assert before == after and before[1] > 20, ("the desktop's sidebar search moved", before, after)
    assert not st["sbAuto"], "the phone-only behaviour switched on at 1280px"


@needs_chrome
def test_the_choice_follows_the_account_to_a_fresh_device():
    async def on_a(b):
        await choose(b, "show")
        await b.until("window.__published.some(e=>(e.tags||[]).some(t=>t[0]==='d'&&t[1]==='pcai:client-prefs'))")
        return await b.js("window.__published")
    published = asyncio.run(device(on_a))
    prefs = _prefs_of(published)
    assert prefs and prefs.get("searchBar") == "show", ("the choice was not saved to the account", prefs)
    seed = [e for e in published if ["d", "pcai:client-prefs"] in [t[:2] for t in e.get("tags", [])]][-1:]

    async def on_b(b):
        out = {"stored": await b.js("ClientSettings.get('searchBar')")}
        await b.js("window.__sbScroll(900)")
        out["down"] = await state(b)
        await open_timeline_settings(b)
        out["select"] = await b.js("document.getElementById('set-search-bar').value")
        return out
    res = asyncio.run(device(on_b, seed=seed))
    assert res["stored"] == "show" and res["select"] == "show", ("a fresh device ignored the account's choice", res)
    assert res["down"]["shown"], ("a fresh device restored 'Always show' but still hid the bar", res["down"])


@needs_chrome
def test_a_change_made_while_the_relays_cannot_answer_is_kept_and_sent_later():
    # One browser process throughout: the "next start" is a reload of the same page, which keeps this
    # device's localStorage (a killed headless Chrome does not reliably flush it to the profile).
    async def act(b):
        out = {}
        await b.js("localStorage.setItem('__sbDark','1')")
        await choose(b, "show")
        await asyncio.sleep(12)               # the save's read gives up: nothing may be published
        await b.js("window.__sbScroll(900)")
        out["dark_published"] = await b.js("window.__published")
        out["stored"] = await b.js("ClientSettings.get('searchBar')")
        out["down"] = await state(b)
        # The relays answer again; the device starts again.
        await b.js("localStorage.setItem('__sbDark','0')")
        await b.call("Page.reload", {})
        await asyncio.sleep(1)
        await boot(b)
        await b.until("window.__published.some(e=>(e.tags||[]).some(t=>t[0]==='d'&&t[1]==='pcai:client-prefs'))")
        out["published"] = await b.js("window.__published")
        return out
    res = asyncio.run(device(act))
    assert not [e for e in res["dark_published"] if (_prefs_of([e]) or {}).get("searchBar")], (
        "the fixture did not keep the relays dark", res["dark_published"])
    assert res["stored"] == "show" and res["down"]["shown"], (
        "the change was lost when the save could not run", res["stored"], res["down"])
    assert (_prefs_of(res["published"]) or {}).get("searchBar") == "show", (
        "the choice made while the relays were dark never reached the account", _prefs_of(res["published"]))
