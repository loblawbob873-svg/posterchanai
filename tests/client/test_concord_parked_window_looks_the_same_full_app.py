"""A Concord desktop window looks the same when another window is focused.

Reported 2026-10-23, three ways, all gone the moment Concord is clicked again:
  * "when I click on another window, custom emoji in chat grows large";
  * "we consolidated the top of the room bar and moved many things to the hamburger menu. When you click
    on another window, they appear like they were before we made the UI change";
  * "when you focus another window and hover over a concord post, the hamburger menu appears as a wide bar".

One cause. The desktop parks an unfocused window by MOVING its nodes into an `.osw-slot`, and the
`body.concord-view` class -- which every rule in concord.css was gated on -- belongs to whichever window
is focused. So the parked Concord window, still on screen, lost its whole stylesheet.

This compares the window ELEMENT BY ELEMENT, focused against parked (every visible element of the room
bar and the message list: displayed or not, and its box), so the next rule that only works while focused
fails here too -- not only the three symptoms that were reported.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_react_full_app import ROOM

PIC = "/static/icon-512.png"   # 512x512 (bundled), the shape of a real pack image

PARKED_ROOM = ROOM.replace(
    "text:'hello from bob',at:Date.now()-60000,kind:9,tags:[]",
    "text:'hello from bob :bigblob: ok',at:Date.now()-60000,kind:9,"
    "tags:[['emoji','bigblob',location.origin+'" + PIC + "']]")

# Every element of the room bar and the messages, keyed by its path inside .cc-app, with its box
# relative to the app (the window does not move when another one is focused).
LAYOUT = r"""(()=>{const app=document.querySelector('.cc-app');if(!app)return null;const o=app.getBoundingClientRect();
  const path=e=>{const p=[];for(let x=e;x&&x!==app;x=x.parentElement){const i=[...x.parentElement.children].indexOf(x);p.unshift(x.tagName+':'+i);}return p.join('/')};
  const out={};for(const root of app.querySelectorAll('.cc-conversation>header,.cc-messages'))
    for(const e of [root,...root.querySelectorAll('*')]){if(e.closest('svg')&&e.tagName!=='svg')continue;
      const r=e.getBoundingClientRect(),cs=getComputedStyle(e);
      out[path(e)]={shown:cs.display!=='none'&&cs.visibility!=='hidden'&&r.width>0&&r.height>0,
        x:Math.round(r.left-o.left),y:Math.round(r.top-o.top),w:Math.round(r.width),h:Math.round(r.height),
        cls:String(e.className&&e.className.baseVal!==undefined?e.className.baseVal:e.className).slice(0,60)};}
  return out;})()"""

EMOJI = """(()=>{const i=[...document.querySelectorAll('.cc-app img')].find(x=>x.src.includes('icon-512'));
  if(!i)return null;const r=i.getBoundingClientRect();return {w:Math.round(r.width),h:Math.round(r.height),loaded:i.complete&&i.naturalWidth>0};})()"""

HOVER = """(()=>{const m=[...document.querySelectorAll('.cc-message')].find(x=>x.textContent.includes('hello from bob'));
  m.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));m.dispatchEvent(new MouseEvent('mouseenter',{bubbles:false}));
  const a=m.querySelector('.cc-message-actions');if(!a)return {none:true};const r=a.getBoundingClientRect(),mr=m.getBoundingClientRect();
  return {w:Math.round(r.width),h:Math.round(r.height),msgW:Math.round(mr.width),shown:getComputedStyle(a).display!=='none'};})()"""


# Give a window a fixed frame (what dragging/resizing does), so the other app can sit BESIDE it and a
# real pointer can reach the parked Concord window.
PLACE = """(()=>{const w=[...document.querySelectorAll('.osw')].find(e=>e.querySelector('.%s'));
  w.classList.remove('max');Object.assign(w.style,{left:'%dpx',top:'40px',width:'740px',height:'760px'});})()"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _hover(b):
    # A REAL pointer over the message (a :hover rule ignores synthetic mouseover events).
    p = await b.js("""(()=>{const m=[...document.querySelectorAll('.cc-message')].find(x=>x.textContent.includes('hello from bob'));
      const r=m.getBoundingClientRect();return {x:r.left+r.width*0.6,y:r.top+r.height/2};})()""")
    await b.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": p["x"], "y": p["y"]})
    await asyncio.sleep(.4)
    return await b.js(HOVER)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_concord_window_is_unchanged_while_another_window_is_focused():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1400, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.until("!!window.PCOS && PCOS.isOn() && !!document.querySelector('#os-desk')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        await b.js(PARKED_ROOM)
        await b.js("__PC.switchView('concord')")   # on the desktop, Communities opens in its own window
        await b.until("!!document.querySelector('#cc-input')")
        await b.js("(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()")
        await b.until("(()=>{const s=" + EMOJI + ";return !!(s&&s.loaded)})()")
        # Concord opens maximised; make it a window that the other app will not cover, so a real
        # pointer can hover it while it is parked.
        await b.js(PLACE % ("cc-app", 0))
        await asyncio.sleep(.8)
        got["focused"] = await b.js(LAYOUT)
        got["emoji_focused"] = await b.js(EMOJI)
        got["hover_focused"] = await _hover(b)
        await b.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 2, "y": 2})
        await b.js("__PC.switchView('notes')")
        await b.until("PCOS.windows().some(w=>w.appView==='notes')")
        await b.until("[...document.querySelectorAll('.osw')].length>=2")
        await b.js("""(()=>{const w=[...document.querySelectorAll('.osw')].find(e=>!e.querySelector('.cc-app'));
          Object.assign(w.style,{left:'760px',top:'40px',width:'600px',height:'760px'});})()""")
        await asyncio.sleep(.8)
        got["parked_really"] = await b.js("!!document.querySelector('.osw-slot .cc-app')")
        got["parked"] = await b.js(LAYOUT)
        got["emoji_parked"] = await b.js(EMOJI)
        got["hover_parked"] = await _hover(b)

    asyncio.run(desktop.with_browser("online", "", check, ""))
    assert got["parked_really"], ("focusing another app did not park the Concord window -- nothing was tested", got.get("parked_really"))
    f, p = got["focused"] or {}, got["parked"] or {}
    assert f, "the Concord window never rendered"
    diffs = []
    for k, a in f.items():
        z = p.get(k)
        if z is None:
            diffs.append((k, a["cls"], "missing when parked"))
        elif a["shown"] != z["shown"] or (a["shown"] and any(abs(a[d] - z[d]) > 2 for d in ("x", "y", "w", "h"))):
            diffs.append((k, a["cls"], {d: (a[d], z[d]) for d in ("shown", "x", "y", "w", "h")}))
    assert not diffs, ("the parked Concord window does not look like the focused one (%d elements differ):\n%s"
                       % (len(diffs), json.dumps(diffs[:25], indent=1)))
    ef, ep = got["emoji_focused"], got["emoji_parked"]
    assert ef and ef["loaded"] and ef["h"] <= 40, ("the custom emoji is not emoji-sized even when focused", ef)
    assert ep and abs(ep["h"] - ef["h"]) <= 1 and abs(ep["w"] - ef["w"]) <= 1, ("the parked window drew the emoji at another size", ef, ep)
    hf, hp = got["hover_focused"], got["hover_parked"]
    assert hf.get("shown") is not None and hp.get("shown") is not None, (hf, hp)
    assert hp == hf, ("hovering a message in the parked window drew its action menu differently", hf, hp)
