"""Settings -> Social -> "Add the emoji packs to my Nostr emoji list": this server's packs reach other Nostr clients.

"make our custom emojis as packs that work on nostr clients" (2026-10-28). The server publishes each pack as a kind-30030
emoji set (tests/test_emoji_sets.py); this is the person's half -- their kind-10030 list, which Amethyst, Nostrudel and
Coracle read. A 10030 is REPLACEABLE, so the three things that matter are what a person would see go wrong:
  * packs they added in another app survive (the list is MERGED, never rebuilt);
  * when their relays do not answer, NOTHING is published and the button says so (a write over an unread list wipes it);
  * pressing it again adds nothing and says they are already there.
The shipped bundle, the real button pressed by id, phone and desktop widths.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

OP = "ab" * 32
SETS = {"pubkey": OP, "relay": "wss://poster.place/relay",
        "sets": [{"d": "pcai-emoji-blobcat", "title": "blobcat", "count": 40},
                 {"d": "pcai-emoji-default", "title": "PosterChan", "count": 12}]}
FOREIGN = "30030:" + "cd" * 32 + ":my-amethyst-pack"

EXTRA = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__publishOK=true;
{const f=window.fetch;window.fetch=async function(url,opts){if(String(url).includes('/client/emoji-sets'))
  return new Response(JSON.stringify(%s),{status:200,headers:{'Content-Type':'application/json'}});return f.apply(this,arguments);};}
// Relays that never answer a 10030 read (no EVENT, no EOSE) when __silent10030 is set.
{const send=WebSocket.prototype.send;WebSocket.prototype.send=function(raw){try{const m=JSON.parse(raw);
  if(window.__silent10030&&m[0]==='REQ'&&m.slice(2).some(f=>(f.kinds||[]).includes(10030)))return;}catch(_){}return send.apply(this,arguments);};}
''' % json.dumps(SETS)



@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _open_social(b, width):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 2 if width < 600 else 1,
                                                         "mobile": width < 600})
    await desktop.login(b)
    await b.until("!!window.__PC_BOOTED")
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js("__PC.switchView('settings')")
    await b.until("!!document.querySelector('.us-tab[data-tab=social]')")
    await b.js("document.querySelector('.us-tab[data-tab=social]').click()")
    await b.until("!!document.querySelector('#us-emoji-sets') && document.querySelector('#us-emoji-sets').getBoundingClientRect().width>0")


async def _press(b):
    x, y = await b.js("(()=>{const e=document.querySelector('#us-emoji-sets');e.scrollIntoView({block:'center'});"
                      "const r=e.getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()")
    await asyncio.sleep(.2)
    for kind in ("mousePressed", "mouseReleased"):
        await b.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1})


async def _said(b):
    await b.until("!/Adding/.test(document.querySelector('#us-emoji-sets-said').textContent) && "
                  "document.querySelector('#us-emoji-sets-said').textContent.length>0")
    return await b.js("document.querySelector('#us-emoji-sets-said').textContent")




@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_the_packs_join_the_list_and_what_was_there_stays(width, tmp_path):
    got = {}

    async def check(b):
        await _open_social(b, width)
        # The person's existing list, signed with their own key (the fixture serves it to the 10030 read).
        await b.js("""(async()=>{const ev=await __PC.signTemplate({kind:10030,created_at:Math.floor(Date.now()/1000)-3600,content:'',
            tags:[['a',%s],['emoji','wave','https://x/wave.png']]});window.__events=[...(window.__events||[]),ev];return true})()""" % json.dumps(FOREIGN))
        await asyncio.sleep(.3)
        got['fits'] = await b.js("(()=>{const r=document.querySelector('#us-emoji-sets').closest('.us-conn').getBoundingClientRect();"
                                 "return r.left>=-1&&r.right<=innerWidth+1})()")
        await b.js("document.querySelector('#us-emoji-sets').scrollIntoView({block:'center'})")
        await asyncio.sleep(.3)
        shot = (await b.call("Page.captureScreenshot", {"format": "png"}))["data"]
        import base64
        import os
        out = Path(os.environ.get("PC_SHOTS") or tmp_path); out.mkdir(parents=True, exist_ok=True)
        Path(out, f"emoji-sets-{width}.png").write_bytes(base64.b64decode(shot))
        await _press(b)
        got['said'] = await _said(b)
        got['pub'] = await b.js("__published.filter(e=>e.kind===10030)")
        # Again: nothing new to add.
        await b.js("window.__events=[...window.__events.filter(e=>e.kind!==10030),...__published.filter(e=>e.kind===10030)];true")
        await _press(b)
        await b.until("/already/.test(document.querySelector('#us-emoji-sets-said').textContent)")
        got['again'] = await b.js("document.querySelector('#us-emoji-sets-said').textContent")
        got['pub2'] = await b.js("__published.filter(e=>e.kind===10030).length")

    asyncio.run(desktop.with_browser("online", "", check, EXTRA))
    assert got['fits'], "the row runs off the screen"
    assert got['said'] == "Added 2 packs.", got['said']
    ev = got['pub'][-1]
    a = [t for t in ev['tags'] if t[0] == 'a']
    assert ['a', FOREIGN] in a, ("a pack the person added in another app was dropped", ev['tags'])
    assert ['emoji', 'wave', 'https://x/wave.png'] in ev['tags'], ("their own single emoji was dropped", ev['tags'])
    assert ['a', f"30030:{OP}:pcai-emoji-blobcat", "wss://poster.place/relay"] in a, a
    assert ['a', f"30030:{OP}:pcai-emoji-default", "wss://poster.place/relay"] in a, a
    assert "already in your list" in got['again'] and got['pub2'] == 1, got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_relays_that_do_not_answer_change_nothing():
    got = {}

    async def check(b):
        await _open_social(b, 390)
        await b.js("window.__silent10030=true")
        await _press(b)
        got['said'] = await _said(b)
        got['pub'] = await b.js("__published.filter(e=>e.kind===10030).length")

    asyncio.run(desktop.with_browser("online", "", check, EXTRA))
    assert got['pub'] == 0, "a list was written over one that was never read"
    assert "did not answer" in got['said'] and "nothing was changed" in got['said'], got['said']
