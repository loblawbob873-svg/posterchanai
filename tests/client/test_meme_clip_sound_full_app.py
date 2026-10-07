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
