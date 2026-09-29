"""A Communities message's "…" menu stays open while the room repaints.

Reported: "the concord post ... menu keeps flashing". Measured: every full repaint — each message that
arrived, each channel refresh — rebuilt the room and dropped `cc-actions-open`, so the open menu shut
under the person using it (and on a phone, with no hover to hold the toolbar up, vanished). Sampled
every animation frame here, with a REAL click on the "…" button.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP

REC = r'''(()=>{window.__vis=[];let on=true;window.__stopVis=()=>on=false;
 const tick=()=>{if(!on)return;const row=document.querySelector('.cc-message[data-message-id^="m050"]');
   __vis.push(row&&row.classList.contains('cc-actions-open')?'O':'x');requestAnimationFrame(tick);};requestAnimationFrame(tick);})()'''


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_the_open_menu_stays_open_through_repaints(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.until("document.querySelectorAll('.cc-message').length>=50 || !!document.querySelector('[data-cc-channel]')")
        await b.js("(()=>{const c=document.querySelector('[data-cc-channel]');if(c&&!document.querySelector('.cc-app.show-chat .cc-message'))c.click();})()")
        await b.until("document.querySelectorAll('.cc-message').length>=50")
        await asyncio.sleep(1.5)
        await b.js("document.querySelector('.cc-message[data-message-id^=\"m050\"]').scrollIntoView({block:'center'})")
        await asyncio.sleep(.3)
        row = await b.js("(()=>{const r=document.querySelector('.cc-message[data-message-id^=\"m050\"]').getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()")
        await b.call("Input.dispatchMouseEvent", dict(type="mouseMoved", x=row[0], y=row[1]))
        await asyncio.sleep(.3)
        t = await b.js("(()=>{const x=document.querySelector('.cc-message[data-message-id^=\"m050\"] [data-cc-actions]');const r=x.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2,r.width]})()")
        assert t[2] > 0, "the … button is not on screen"
        for k in ("mousePressed", "mouseReleased"):
            await b.call("Input.dispatchMouseEvent", dict(type=k, x=t[0], y=t[1], button="left", clickCount=1))
        await b.until("document.querySelector('.cc-message[data-message-id^=\"m050\"]').classList.contains('cc-actions-open')")
        await b.js(REC)
        for _ in range(6):
            await b.js("PCConcord.render()")
            await asyncio.sleep(.25)
        vis = await b.js("__stopVis(),__vis.join('')")
        assert vis and set(vis) == {"O"}, f"the open menu closed during repaints: {vis}"

    asyncio.run(desktop.with_browser("online", "", check))
