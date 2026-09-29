"""Communities: opening a reply thread shows its "← Back · Thread" bar ACROSS THE TOP, not beside it.

Reported: "Communities, replying to a post has a big bug, weird thread button with a back button
appears, messes up the display". The messages pane is a flex ROW (it bottom-aligns the list), so the
thread bar became a narrow column next to the messages — in the middle of a desktop screen, and on a
phone it squeezed the whole thread into a strip. Driven for real: reply to a post, open the thread.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


GEOM = r"""(()=>{const r=s=>document.querySelector(s).getBoundingClientRect();const bar=r('.cc-thread-bar'),list=r('.cc-message-list'),box=r('.cc-messages');
  return {barW:Math.round(bar.width),boxW:Math.round(box.width),listW:Math.round(list.width),barBottom:Math.round(bar.bottom),listTop:Math.round(list.top)}})()"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 390])
def test_the_thread_bar_is_a_header_across_the_thread(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=850, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.js("(()=>{const c=document.querySelector('[data-cc-channel]');if(c&&!document.querySelector('.cc-app.show-chat .cc-message'))c.click();})()")
        await b.until("document.querySelectorAll('.cc-message').length>=50")
        await b.js("document.querySelector('.cc-message[data-message-id^=\"m050\"] [data-cc-reply]').click()")
        await b.js("(()=>{const i=document.querySelector('#cc-input');i.value='my reply text';i.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await b.js("document.querySelector('#cc-send').click()")
        await b.until("!!document.querySelector('[data-cc-thread]')")
        await b.js("document.querySelector('[data-cc-thread]').click()")
        await b.until("!!document.querySelector('.cc-thread-bar')")
        g = await b.js(GEOM)
        assert g["barW"] >= 0.9 * g["boxW"] - 40, ("the thread bar is a column, not a header", g)
        assert g["barBottom"] <= g["listTop"] + 1, ("the thread bar is beside the messages, not above them", g)
        assert g["listW"] >= 0.8 * g["boxW"] - 40, ("the thread is squeezed into a strip", g)
        await b.js("document.querySelector('#cc-thread-back').click()")
        await b.until("!document.querySelector('.cc-thread-bar')")

    asyncio.run(desktop.with_browser("online", "", check))
