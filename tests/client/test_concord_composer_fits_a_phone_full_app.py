"""Concord's room composer and its upload toast, measured on a phone.

Owner, from the APK: "concord on apk: send button not centered vertically in room chat, touching
bottom, also, touching message box" -- and then "uploading toaster does not fit on screen in concord".

Both were geometry nobody measured. Measured before the fix, at 390x844 (Cyberpunk, Classic, Monero
alike, since the cause is not in any theme token):

* The textarea carried the global `textarea{margin:6px 0}`, which made the composer row 54px tall
  while every control in it was 40-42px. With `align-items:flex-end` the 40px Send button sat on the
  row's floor, 7px BELOW the input's centre and 5px off the bar's border -- "touching bottom" -- and
  the row gap was 2px, so it touched whatever was beside it ("touching message box"; at 360px the
  emoji button is hidden and Send sits directly against the textarea).
* `#toast-root` was `left:50%` + `translateX(-50%)` with no width limit, so "uploading
  <a long camera file name>…" was as wide as its text and ran off BOTH edges of the phone, and at
  `bottom:20px` it sat on top of the composer and the gesture bar.

Driven through the real bundled client, a real login and a real Concord room (only the relay and the
CORD reader are fixtures), with a phone UA, touch and a 24px safe-area-inset-bottom like a gesture-nav
phone. The upload toast is produced by the real attach path (a File handed to #cc-file).
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


THEMES = ["cyberpunk", "classic", "synthwave", "monero", "light", "oled", "app"]
PHONE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0 Mobile Safari/537.36")
SAFE_BOTTOM = 24
LONG_NAME = "PXL_20261011_174512345_MP_PORTRAIT_ORIGINAL_from_the_family_trip_to_the_coast.jpg"

# Send, the control just before it (the textarea, or the emoji button where that is shown), the
# textarea and the bar, in viewport px.
GEOM = r"""(()=>{const box=e=>{const b=e.getBoundingClientRect();return {l:b.left,t:b.top,r:b.right,b:b.bottom,h:b.height};};
  const send=document.querySelector('#cc-send');let prev=send.previousElementSibling;
  while(prev&&prev.getBoundingClientRect().width===0)prev=prev.previousElementSibling;
  /* In CSS px of the composer: the desktop shell scales the page (body zoom), a phone does not. */
  const z=send.getBoundingClientRect().height/send.offsetHeight||1,box2=e=>{const b=box(e);for(const k in b)b[k]/=z;return b;};
  return {bar:box2(document.querySelector('.cc-compose')),input:box2(document.querySelector('#cc-input')),
    send:box2(send),prev:box2(prev),prevId:prev.id,vh:innerHeight/z,zoom:z};})()"""

SET_TEXT = "(v=>{const i=document.querySelector('#cc-input');i.value=v;i.dispatchEvent(new Event('input',{bubbles:true}));})"


async def open_room(b, width, height):
    phone = width < 600
    await b.call("Emulation.setDeviceMetricsOverride",
                 dict(width=width, height=height, deviceScaleFactor=2 if phone else 1, mobile=phone))
    if phone:
        await b.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        await b.call("Emulation.setUserAgentOverride", {"userAgent": PHONE_UA})
        await b.call("Emulation.setSafeAreaInsetsOverride", {"insets": {"bottom": SAFE_BOTTOM}})
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js(SETUP)
    await b.until("!!document.querySelector('[data-cc-channel]')||document.querySelectorAll('.cc-message').length>=50")
    await b.js("(()=>{const c=document.querySelector('[data-cc-channel]');if(c&&!document.querySelector('.cc-app.show-chat .cc-message'))c.click();})()")
    await b.until("document.querySelectorAll('.cc-message').length>=50 && !!document.querySelector('#cc-send')")
    await asyncio.sleep(.3)


def composer_problems(g, where):
    """Every way Send is out of place, so one failure reports all of them."""
    s, prev, bar = g["send"], g["prev"], g["bar"]
    out = []
    if s["l"] - prev["r"] < 6:
        out.append(f"{where}: Send touches #{g['prevId']} ({s['l'] - prev['r']:.1f}px apart)")
    if bar["b"] - s["b"] < 6:
        out.append(f"{where}: Send is {bar['b'] - s['b']:.1f}px off the composer's bottom edge")
    if bar["r"] - s["r"] < 6:
        out.append(f"{where}: Send is {bar['r'] - s['r']:.1f}px off the composer's right edge")
    if s["t"] - bar["t"] < 6:
        out.append(f"{where}: Send is {s['t'] - bar['t']:.1f}px off the composer's top edge")
    if g["phone"] and g["vh"] - bar["b"] < SAFE_BOTTOM + 4:
        out.append(f"{where}: the composer sits in the gesture area ({g['vh'] - bar['b']:.1f}px from the bottom)")
    return out


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,height", [(360, 780), (390, 844), (1280, 850)])
def test_concord_send_is_centred_with_room_around_it_in_every_theme(width, height):
    async def check(b):
        await open_room(b, width, height)
        problems, samples = [], {}
        for theme in THEMES:
            await b.js(f"PCConcordTheme.set({{theme:{json.dumps(theme)}}},{{save:false}})")
            await asyncio.sleep(.15)
            await b.js(SET_TEXT + "('')")
            await asyncio.sleep(.1)
            one = await b.js(GEOM)
            one["phone"] = width < 600
            where = f"{width}px {theme} single line"
            centre = (one["send"]["t"] + one["send"]["b"]) / 2 - (one["input"]["t"] + one["input"]["b"]) / 2
            if abs(centre) > 2:
                problems.append(f"{where}: Send's centre is {centre:.1f}px off the input's")
            problems += composer_problems(one, where)

            await b.js(SET_TEXT + "('line one\\nline two\\nline three\\nline four')")
            await asyncio.sleep(.1)
            multi = await b.js(GEOM)
            multi["phone"] = width < 600
            where = f"{width}px {theme} several lines"
            problems += composer_problems(multi, where)
            # Pinned to the bottom line with the same inset it had on one line -- never drifting.
            drift = (multi["bar"]["b"] - multi["send"]["b"]) - (one["bar"]["b"] - one["send"]["b"])
            if abs(drift) > 1:
                problems.append(f"{where}: Send moved {drift:.1f}px off the bottom line")
            samples[theme] = one
        await b.js(SET_TEXT + "('')")
        assert not problems, ("\n".join(problems), samples["cyberpunk"])

    asyncio.run(desktop.with_browser("online", "", check))


TOAST = r"""(()=>{const t=[...document.querySelectorAll('#toast-root .toast')].find(e=>/^uploading /.test(e.textContent));
  if(!t)return null;const r=t.getBoundingClientRect(),bar=document.querySelector('.cc-compose').getBoundingClientRect();
  return {l:r.left,r:r.right,t:r.top,b:r.bottom,barTop:bar.top,vw:document.documentElement.clientWidth,vh:innerHeight,
    clipped:t.scrollWidth>t.clientWidth+1};})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,height", [(360, 780), (390, 844)])
def test_concord_upload_toast_with_a_long_name_fits_the_phone(width, height):
    async def check(b):
        await open_room(b, width, height)
        await b.js("(()=>{const f=new File([new Uint8Array(2048)]," + json.dumps(LONG_NAME) + ",{type:'image/jpeg'}),"
                   "dt=new DataTransfer();dt.items.add(f);const i=document.querySelector('#cc-file');i.files=dt.files;"
                   "i.dispatchEvent(new Event('change',{bubbles:true}));})()")
        await b.until(TOAST.replace("if(!t)return null;", "if(!t)return false;return true;"))
        g = await b.js(TOAST)
        assert g["l"] >= 12 - .5 and g["r"] <= g["vw"] - 12 + .5, ("the upload toast runs off the screen", g)
        assert not g["clipped"], ("the file name is cut off inside the toast", g)
        assert g["b"] <= g["barTop"], ("the upload toast covers the composer", json.dumps(g))
        assert g["vh"] - g["b"] >= SAFE_BOTTOM, ("the upload toast sits in the gesture area", g)

    asyncio.run(desktop.with_browser("online", "", check))


TYPED = r"""(()=>{const i=document.querySelector('#cc-input'),s=getComputedStyle(i);
  return {focused:document.activeElement===i,outline:s.outlineStyle==='none'?0:parseFloat(s.outlineWidth)||0,
    shadow:s.boxShadow,border:parseFloat(s.borderTopWidth)||0};})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,height", [(390, 844), (1280, 850)])
def test_typing_a_message_draws_no_second_box_inside_the_composer(width, height):
    """ "there is a rectangle box when you enter your message in the room ... so everywhere": a textarea is
    :focus-visible whenever it is focused, and Concord's focus ring (every theme but Classic) drew an outline +
    glow around it INSIDE the composer bar. Tapped/clicked and typed into for real, in every theme."""
    async def check(b):
        await open_room(b, width, height)
        problems = []
        for theme in THEMES:
            await b.js(f"PCConcordTheme.set({{theme:{json.dumps(theme)}}},{{save:false}})")
            await b.js("document.activeElement&&document.activeElement.blur()")
            await asyncio.sleep(.15)
            x, y = await b.js("(()=>{const r=document.querySelector('#cc-input').getBoundingClientRect();"
                              "return [r.left+r.width/2,r.top+r.height/2]})()")
            if width < 600:
                await b.call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
                await b.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            else:
                for t in ("mousePressed", "mouseReleased"):
                    await b.call("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1})
            await b.call("Input.insertText", {"text": "hello"})
            await asyncio.sleep(.15)
            g = await b.js(TYPED)
            if not g["focused"]:
                problems.append(f"{theme}: the tap did not focus the message box")
            if g["outline"] or g["border"] or (g["shadow"] and g["shadow"] != "none"):
                problems.append(f"{theme}: typing draws a box around the message ({g})")
            await b.js(SET_TEXT + "('')")
        assert not problems, "\n".join(problems)

    asyncio.run(desktop.with_browser("online", "", check))
