"""Meme Builder: a video clip's sound is muted or turned up/down RIGHT UNDER THE CLIP, not in Timing.

Reported: "Meme builder, need way to mute video clip … or adjust volume" -- "ah I see it in timing, bad place".
The mute was a checkbox inside the collapsed ⏱ Timing group and a video clip had no volume control at all,
though the renderer has always honoured both (meme_builder_service: `mute`, `volume` 0-4). Asserted in the
shipped client: with a video layer selected, a mute button and a volume slider are visible without opening any
group; the button mutes and unmutes the clip in the saved project (which is what the export sends), the slider
sets its volume and shows the level, and Timing no longer carries the mute.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


LAYER = "(JSON.parse(localStorage.getItem('pc_meme_project')||'{}').layers||[]).find(l=>l.type==='video')"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_a_video_clip_has_mute_and_volume_right_under_it(width):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 500})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("localStorage.removeItem('pc_meme_project'); __PC.switchView('meme'); true")
        await b.until("!!window.PCMeme")
        await b.js("PCMeme.addMedia('https://media.fixture.invalid/clip.mp4','video/mp4'); true")
        # Select it (a new layer is usually selected already; clicking its timeline row makes sure).
        for _ in range(100):
            if await b.js("!!document.getElementById('mb-f-vvol')"):
                break
            await b.js("(document.querySelector('.mb-row[data-id], .mb-lrow[data-id], [data-layer]')||{click(){}}).click(); true")
            await asyncio.sleep(.1)
        got["visible"] = await b.js("""(()=>{const m=document.getElementById('mb-f-mute'), v=document.getElementById('mb-f-vvol');
          const shown=e=>!!e && e.getClientRects().length>0 && !e.closest('details:not([open])');
          return {mute:shown(m), vol:shown(v), inTiming:!!(m&&m.closest('[data-sec="time"]'))}; })()""")
        got["timing_has_mute"] = await b.js("!!document.querySelector('[data-sec=\"time\"] #mb-f-mute, [data-sec=\"time\"] input[type=checkbox]#mb-f-mute')")
        await b.js("document.getElementById('mb-f-mute').click(); true")
        await asyncio.sleep(.3)
        got["muted"] = await b.js(LAYER + ".mute")
        got["label"] = await b.js("document.getElementById('mb-f-mute').textContent")
        got["slider_disabled"] = await b.js("document.getElementById('mb-f-vvol').disabled")
        await b.js("document.getElementById('mb-f-mute').click(); true")
        await asyncio.sleep(.3)
        got["unmuted"] = await b.js(LAYER + ".mute")
        await b.js("(()=>{const s=document.getElementById('mb-f-vvol'); s.value='0.35'; s.dispatchEvent(new Event('input',{bubbles:true}));})(); true")
        await asyncio.sleep(.3)
        got["volume"] = await b.js(LAYER + ".volume")
        got["shown_level"] = await b.js("document.getElementById('mb-vvol-val').textContent")
        got["overflow"] = await b.js("document.documentElement.scrollWidth > window.innerWidth + 1")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["visible"]["mute"] and got["visible"]["vol"], ("the clip's sound controls are hidden", got)
    assert not got["visible"]["inTiming"] and not got["timing_has_mute"], ("mute is still in Timing", got)
    assert got["muted"] is True and "Muted" in got["label"] and got["slider_disabled"] is True, got
    assert got["unmuted"] is False, got
    assert abs(got["volume"] - 0.35) < 1e-6 and got["shown_level"] == "35%", got
    assert not got["overflow"], "the page scrolls sideways"


# Before any page script: the effect catalogue lists `mentioned`, and the apply-effect render is captured.
EFFECTS_STUB = r"""(()=>{ const real=window.fetch; window.__fx=[];
  window.fetch=(u,o)=>{ const s=String(u);
    if(s.endsWith('/client/effects')) return Promise.resolve(new Response(JSON.stringify({enhance:[],effects:[{name:'mentioned',desc:'x'}]}),{status:200,headers:{'Content-Type':'application/json'}}));
    if(s.includes('/client/meme/apply-effect')){ __fx.push(JSON.parse(o.body)); return Promise.resolve(new Response(JSON.stringify({detail:'stub'}),{status:500,headers:{'Content-Type':'application/json'}})); }
    return real(u,o); }; })();"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_mentioned_effect_asks_for_its_word_and_sends_it():
    """"how will users be able to set the text for mentioned" -- `mentioned` has no default caption, so the Meme
    Builder's effect menu asks for the word in its own dialog and sends it as the effect's arg."""
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("localStorage.removeItem('pc_meme_project'); __PC.switchView('meme'); true")
        await b.until("!!window.PCMeme")
        await b.js("PCMeme.addMedia('https://media.fixture.invalid/pic.png','image/png'); true")
        for _ in range(100):
            if await b.js("!!document.getElementById('mb-f-meme')"):
                break
            await b.js("(document.querySelector('.mb-row[data-id], .mb-lrow[data-id], [data-layer]')||{click(){}}).click(); true")
            await asyncio.sleep(.1)
        await b.js("(()=>{const s=document.getElementById('mb-f-meme'); s.value='mentioned'; s.dispatchEvent(new Event('change',{bubbles:true}));})();true")
        await b.until("!!document.querySelector('.uiprompt-in')")
        got["asked"] = await b.js("document.querySelector('.uiconfirm-msg').textContent")
        await b.js("(()=>{const i=document.querySelector('.uiprompt-in'); i.value='pizza'; i.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('[data-uc=\"1\"]').click();})();true")
        for _ in range(100):
            if await b.js("__fx.length>0"):
                break
            await asyncio.sleep(.1)
        got["sent"] = await b.js("__fx[0]||null")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=EFFECTS_STUB))
    assert "mentioned" in got["asked"].lower(), got
    assert got["sent"] and got["sent"]["effect"] == "mentioned" and got["sent"].get("arg") == "pizza", got


OVERLAY_STUB = r"""(()=>{ const real=window.fetch;
  window.fetch=(u,o)=>{ const s=String(u);
    if(s.endsWith('/client/meme/effects')) return Promise.resolve(new Response(JSON.stringify({effects:[{name:'mentioned',label:'🎉 Mentioned (cheering)',audio:true}]}),{status:200,headers:{'Content-Type':'application/json'}}));
    if(s.includes('/client/meme/effect') && !s.includes('apply-effect')) return Promise.resolve(new Response(JSON.stringify({url:'https://media.fixture.invalid/cheer.webm',dur:3,sound:'mentioned'}),{status:200,headers:{'Content-Type':'application/json'}}));
    return real(u,o); }; })();"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_adding_the_mentioned_girl_asks_for_the_word_and_adds_it_as_a_caption():
    """"make sure it works in meme builder as well, maybe user can manually add text for that" -- the cheering
    girl as an overlay clip asks what got mentioned and adds "<THING> MENTIONED" as an ordinary, editable text
    layer timed with her."""
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("localStorage.removeItem('pc_meme_project'); __PC.switchView('meme'); true")
        await b.until("!!window.PCMeme && !!document.getElementById('mb-add-effect')")
        await asyncio.sleep(.5)
        await b.js("document.getElementById('mb-add-effect').click();true")
        await b.until("[...document.querySelectorAll('button[data-i]')].some(x=>x.textContent.includes('Mentioned'))")
        await b.js("[...document.querySelectorAll('button[data-i]')].find(x=>x.textContent.includes('Mentioned')).click();true")
        await b.until("!!document.querySelector('.uiprompt-in')")
        await b.js("(()=>{const i=document.querySelector('.uiprompt-in'); i.value='pizza'; i.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('[data-uc=\"1\"]').click();})();true")
        for _ in range(100):
            if await b.js("(JSON.parse(localStorage.getItem('pc_meme_project')||'{}').layers||[]).some(l=>l.type==='text')"):
                break
            await asyncio.sleep(.1)
        got["layers"] = await b.js("(JSON.parse(localStorage.getItem('pc_meme_project')||'{}').layers||[]).map(l=>({type:l.type,text:l.text||'',start:l.start,dur:l.dur}))")

    asyncio.run(desktop.with_browser("online", "", check, extra_init=OVERLAY_STUB))
    vids = [l for l in got["layers"] if l["type"] == "video"]
    texts = [l for l in got["layers"] if l["type"] == "text"]
    assert vids, ("the cheering clip was not added", got)
    assert texts and texts[0]["text"] == "PIZZA MENTIONED", ("no editable caption with the word", got)
    assert texts[0]["start"] == vids[0]["start"] and texts[0]["dur"] == vids[0]["dur"], ("the caption is not timed with her", got)
