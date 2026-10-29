"""Social timeline: a lone picture sits in one consistent frame, whatever its shape.

Reported: "Social Timelines -> Media preview sizes are not consistent." Measured at 390px before: a lone landscape
photo drew 134px tall, a portrait one 675px (most of a phone screen), a square 312px, while a post with 2+ photos is a
fixed 200px strip -- heights swinging 5x from card to card. Now a lone picture fills the column inside a frame clamped
between 16:9 and 4:5 (and at most 520px on a wide screen); a carousel is unchanged. Real images of each shape from
static/, the shipped client, phone and desktop widths.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


POSTS = {
  'landscape': ['/static/signon-nostr-city.png'],
  'portrait':  ['/static/screenshot-mobile.png'],
  'square':    ['/static/icon-512.png'],
  'card':      ['/static/share-card.png'],
  'two':       ['/static/signon-nostr-city.png', '/static/screenshot-mobile.png'],
  'five':      ['/static/signon-nostr-city.png', '/static/screenshot-mobile.png', '/static/icon-512.png', '/static/share-card.png', '/static/favicon.png'],
}
RELAY = r'''
window.__made=null;
function __mk(){ if(window.__made) return window.__made; const NT=window.NostrTools, now=Math.floor(Date.now()/1000), o=location.origin;
  const k=Uint8Array.from({length:32},(_,j)=>j===31?9:(j===30?1:0)); const P=%s; const ev=[]; let i=0;
  for(const [name,imgs] of Object.entries(P)){ ev.push(NT.finalizeEvent({kind:1,created_at:now-60-(i++)*60,tags:[],content:'MEDIA '+name+' '+imgs.map((u,j)=>o+u+'?'+name+j).join(' ')},k)); }
  return (window.__made={ev}); }
class FakeSocket{constructor(u){this.url=u;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;} if(m[0]!=='REQ')return; const id=m[1];
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});}; const D=__mk();
  for(const f of m.slice(2)){ if((f.kinds||[]).includes(1)&&!f.ids&&!f.authors&&!f['#e']&&!f['#p']){ D.ev.forEach(e=>out(['EVENT',id,e])); out(['EOSE',id]); return; } }
  out(['EOSE',id]); }
 close(){this.readyState=3;} }
FakeSocket.OPEN=1;FakeSocket.CONNECTING=0;FakeSocket.CLOSED=3; window.WebSocket=FakeSocket;
''' % json.dumps(POSTS)

MEASURE = r'''(()=>{const out={};for(const n of document.querySelectorAll('#tl-notes .note')){const t=n.textContent;const m=/MEDIA (\w+)/.exec(t);if(!m)continue;
  const body=n.querySelector('.body')||n;const bw=Math.round(body.getBoundingClientRect().width);
  const car=n.querySelector('.media-car');
  const imgs=[...n.querySelectorAll('img')].filter(i=>!i.closest('.av')&&!i.classList.contains('av')&&!i.classList.contains('emoji-inline')&&i.getBoundingClientRect().width>40);
  out[m[1]]={col:bw,carousel:car?{w:Math.round(car.getBoundingClientRect().width),h:Math.round(car.getBoundingClientRect().height)}:null,
    imgs:imgs.map(i=>{const r=i.getBoundingClientRect();return [Math.round(r.width),Math.round(r.height),i.naturalWidth&&+(i.naturalWidth/i.naturalHeight).toFixed(2),i.parentElement.className.slice(0,30),
      (()=>{if(!i.naturalWidth)return null;const fit=getComputedStyle(i).objectFit,sx=r.width/i.naturalWidth,sy=r.height/i.naturalHeight;
        /* the share of the picture NOT drawn: cover scales to the larger factor and trims the overflow */
        return fit==='cover'?+(1-Math.min(sx,sy)/Math.max(sx,sy)).toFixed(3):(fit==='fill'?null:0);})()]})};}
  return out;})()'''



async def _measure(width, os_mode=False):
    res = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 1080 if os_mode else 900, "deviceScaleFactor": 1, "mobile": width < 821})
        await desktop.login(b)
        if not os_mode:
            await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('global')")
        await b.until("document.querySelectorAll('#tl-notes .note').length>=6")
        await asyncio.sleep(2)
        res.update(await b.js(MEASURE))
    await desktop.with_browser("online", "", check, RELAY)
    return res


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_a_lone_picture_gets_one_frame_whatever_its_shape(width):
    got = asyncio.run(_measure(width))
    singles = {k: got[k]["imgs"][0] for k in ("landscape", "portrait", "square", "card")}
    col = got["landscape"]["col"]
    for name, (w, h, *_rest) in singles.items():
        assert abs(w - col) <= 2, (name, "a lone picture does not span the column", w, col)
        # "we should not be cropping notes on the timeline, that is terrible" (2026-10-28): the WHOLE picture is drawn.
        assert _rest[-1] == 0, (name, "part of the picture is trimmed off (share not drawn)", _rest[-1])
        assert h >= 0.55 * col - 2, (name, "wider than 16:9 -- a sliver of a picture", h, col)
        assert h <= 1.26 * col + 2, (name, "taller than 4:5 -- most of a phone screen", h, col)
    heights = [h for (_w, h, *_r) in singles.values()]
    assert max(heights) / min(heights) <= 2.3, ("lone pictures still swing in height", singles)
    assert got["two"]["carousel"] and got["five"]["carousel"]["h"] == got["two"]["carousel"]["h"], got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_lone_picture_is_not_gigantic_in_a_desktop_window():
    """2026-10-22, "on Desktop/OS, in social, images are gigantic! probably videos too": in a desktop window the column
    is the window (1322px on screen at 1920), and the frame above took 100% of it with min-height 56.25% of it --
    1322x744 for every lone picture, landscape or portrait. The test above left the desktop (PCOS.exit) and never saw
    it. The frame is capped at 840px (CSS px; the window's zoom makes it smaller on screen) and keeps 16:9..4:5."""
    got = asyncio.run(_measure(1920, os_mode=True))
    col = got["landscape"]["col"]
    singles = {k: got[k]["imgs"][0] for k in ("landscape", "portrait", "square", "card")}
    for name, (w, h, *_rest) in singles.items():
        assert w <= 0.6 * col, (name, "a lone picture spans the whole desktop window", w, col)
        assert 0.55 * w - 2 <= h <= 1.26 * w + 2, (name, "outside 16:9..4:5", w, h)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_a_post_with_several_pictures_shows_each_one_whole(width):
    """2026-10-28, "images in notes are still cropped? we should not be cropping anywhere": the lone picture was
    fixed, but a post with 2+ pictures draws a two-at-a-time strip whose cells were object-fit:cover -- a portrait
    phone screenshot lost most of its height. Each cell now letterboxes its picture whole."""
    got = asyncio.run(_measure(width))
    for name in ("two", "five"):
        shown = [i for i in got[name]["imgs"] if i[2]]
        assert shown, (name, "no decoded picture in the strip", got[name])
        for (w, h, ratio, cls, trimmed) in shown:
            assert trimmed == 0, (name, "a picture in the strip is trimmed", w, h, ratio, cls, trimmed)
