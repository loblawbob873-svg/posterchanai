"""Communities: an encrypted video does not keep moving the chat.

Reported live in the Lounge: "video in room Lounge causes chat position to keep moving". The hydrator
rebuilt every encrypted <video> after EVERY repaint, and a live room repaints constantly (messages,
profiles, typing): each time the player collapsed to nothing, reloaded and grew back, shoving the
chat around it. Driven in the shipped client with a REAL encrypted video (a tiny webm made with
ffmpeg, sealed with AES-GCM exactly as Concord seals attachments): once shown and playing, ten repaints
and a new message (a real rebuild) must leave the SAME <video> element in place and still playing, its
box must never collapse, and the message below it must not move.
"""
import asyncio
import base64
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def _tiny_webm() -> bytes:
    # A ten-second 320x180 VP8 clip (long enough to never wrap during the test: a LOOP fires 'waiting' at 0:00, which is not a repaint), COMMITTED rather than made with ffmpeg at test time: the desktop CI
    # host has no ffmpeg, and there a skip counts as a failure (required coverage), which blocked the
    # deploy-79 desktop build. Regenerate with:
    #   ffmpeg -f lavfi -i testsrc=size=320x180:rate=10 -t 10 -c:v libvpx -b:v 30k tests/fixtures/concord_clip.webm
    return (Path(__file__).resolve().parents[1] / "fixtures" / "concord_clip.webm").read_bytes()


SEAL = r"""(async()=>{
  const plain=Uint8Array.from(atob(%r),c=>c.charCodeAt(0));
  const hex=b=>[...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('');
  const kb=crypto.getRandomValues(new Uint8Array(32)), nonce=crypto.getRandomValues(new Uint8Array(16));
  const key=await crypto.subtle.importKey('raw',kb,'AES-GCM',false,['encrypt']);
  const cipher=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv:nonce},key,plain));
  const ox=hex(await crypto.subtle.digest('SHA-256',plain)), url='https://blossom.fixture.invalid/'+ox;
  window.__imeta=['imeta','url '+url,'m video/webm','encryption-algorithm aes-gcm','decryption-key '+hex(kb),'decryption-nonce '+hex(nonce),'ox '+ox,'name clip.webm'];
  const real=window.fetch.bind(window);
  window.fetch=(u,o)=>String(u&&u.url||u)===url?Promise.resolve(new Response(cipher,{status:200})):real(u,o);
  return true;})()"""

STATE = r"""(()=>{const v=document.querySelector('.cc-encrypted-attachment video'), host=v&&v.closest('.cc-encrypted-attachment');
  const msg=host&&host.closest('.cc-message'), below=msg&&msg.nextElementSibling;
  return {has:!!v, same:!!v&&v===window.__vid, art:!!msg&&msg===window.__art, h:host?Math.round(host.getBoundingClientRect().height):0,
          below:below?Math.round(below.getBoundingClientRect().top-msg.getBoundingClientRect().top):null,
          meta:!!v&&v.readyState>=1}})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_encrypted_video_stays_put_while_the_room_repaints():
    clip = base64.b64encode(_tiny_webm()).decode()
    res = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1280, height=850, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SEAL % clip)
        await b.js(SETUP)
        await b.js("(()=>{const c=document.querySelector('[data-cc-channel]');if(c&&!document.querySelector('.cc-app.show-chat .cc-message'))c.click();})()")
        await b.until("document.querySelectorAll('.cc-message').length>=50")
        await b.js("document.querySelector('.cc-encrypted-attachment')&&document.querySelector('.cc-encrypted-attachment').scrollIntoView({block:'center'});true")
        await b.until("!!document.querySelector('.cc-encrypted-attachment video')")
        await b.until("(()=>{const v=document.querySelector('.cc-encrypted-attachment video');return !!v&&v.readyState>=1})()")
        await asyncio.sleep(.5)
        for _ in range(3):                        # its size is now known; let the room settle
            await b.js("PCConcord.render();true")
            await asyncio.sleep(.15)
        await b.js("window.__vid=document.querySelector('.cc-encrypted-attachment video');window.__art=__vid.closest('.cc-message');__vid.muted=true;__vid.play().catch(()=>{});true")
        await asyncio.sleep(.6)
        # What the person sees as "the circle reloads": the player reloading, stalling or pausing.
        await b.js("window.__ev=[];['loadstart','emptied','waiting','pause','abort'].forEach(t=>__vid.addEventListener(t,()=>__ev.push(t)));true")
        await asyncio.sleep(.4)
        first = await b.js(STATE)
        seen = []
        for _ in range(10):                       # what a live room does: repaint, again and again
            await b.js("PCConcord.render();true")
            await asyncio.sleep(.15)
            seen.append(await b.js(STATE))
        # A new message rebuilds the conversation for real -- the player must survive it, still playing.
        await b.js("const i=document.getElementById('cc-input');i.value='a new message arrives';i.dispatchEvent(new Event('input'));document.getElementById('cc-send').click();true")
        await b.until("[...document.querySelectorAll('.cc-message')].some(m=>/a new message arrives/.test(m.textContent))")
        await asyncio.sleep(.3)
        seen.append(await b.js(STATE))
        # A LIVE TICK WHILE SOMEBODY IS IN THE COMPOSER -- the ordinary state right after sending. That
        # path (backgroundRender -> patchMessageList) repainted the message pane with innerHTML and never
        # carried the players over: every tick a new <video>, the loading circle, every few seconds
        # ("the concord video in Lounge room keeps glitching every few seconds, you see the circle reload").
        await b.js("document.getElementById('cc-input').focus();true")
        for _ in range(4):
            await b.js("PCConcord.backgroundRender();true")
            await asyncio.sleep(.2)
            seen.append(await b.js(STATE))
        res["focused"] = await b.js("document.activeElement&&document.activeElement.id")
        res["playing"] = await b.js("!!window.__vid&&!__vid.paused&&__vid.isConnected")
        res["events"] = await b.js("__ev")
        res.update(first=first, seen=seen)

    asyncio.run(desktop.with_browser("online", "", check))
    first, seen = res["first"], res["seen"]
    assert res["focused"] == "cc-input", res["focused"]
    assert first["has"] and first["meta"] and first["h"] > 50, ("the video never showed", first)
    assert all(s["same"] for s in seen), ("a repaint replaced the video player (it reloads and the chat jumps)", seen)
    # Not even detached and put back: a moved player can re-buffer. The message holding it stays the same node.
    assert all(s["art"] for s in seen), ("a repaint detached the message holding the video", seen)
    assert min(s["h"] for s in seen) >= first["h"] - 2, ("the video box collapsed during a repaint", first, seen)
    assert len({s["below"] for s in seen}) == 1 and seen[0]["below"] == first["below"], ("the message below the video moved", first, seen)
    assert res["playing"], "a new message stopped the video someone was watching"
    # 'the concord video in Lounge room keeps glitching every few seconds, you see the circle reload'
    assert res["events"] == [], ("a repaint made the player reload, stall or pause (the loading circle)", res["events"])
