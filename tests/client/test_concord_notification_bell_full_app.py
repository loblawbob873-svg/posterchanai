"""The Communities bell turns notifications OFF as well as on (2026-10-21: "only says enabled, never disabled").

It only ever asked the OS for permission and reported the answer. Now it is a per-account switch: off means no
OS notification from Concord (mentions, repository activity) -- the bell says so (crossed-out icon,
aria-pressed=false) and the setting survives a reload. Real bundled client, phone and desktop width.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


async def press(b, sel):
    t = await b.js("(()=>{const e=document.querySelector(%r);e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;return {x,y,hit:r.width>0&&e.contains(document.elementFromPoint(x,y))};})()" % sel)
    assert t["hit"], f"{sel} is hidden or covered"
    for k in ("mousePressed", "mouseReleased"):
        await b.call("Input.dispatchMouseEvent", dict(type=k, x=t["x"], y=t["y"], button="left", clickCount=1))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [1280, 393])
def test_the_bell_switches_community_notifications_off_and_on(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=851, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; __PC.askOsNotify=async()=>'granted'; __PC.switchView('concord'); true")
        await b.until("!!document.querySelector('#cc-notify')")
        import base64, os
        if os.environ.get("PC_SHOTS"):
            d = await b.call("Page.captureScreenshot", dict(format="png"))
            Path(os.environ["PC_SHOTS"], f"bell-{width}.png").write_bytes(base64.b64decode(d["data"]))
        async def tap_bell():
            if width < 600:   # a phone on the Concord home never sees the drawer footer: its header has a bell
                await press(b, "#cc-notify-head")
            else:
                await press(b, "#cc-notify")
        assert await b.js("document.querySelector('#cc-notify').getAttribute('aria-pressed')") == "true"
        # The Concord home has no community and no channel: no "Message #general" composer to nobody.
        assert await b.js("document.querySelector('.cc-app').classList.contains('home-view')")
        assert await b.js("getComputedStyle(document.querySelector('.cc-compose')).display") == "none"
        await tap_bell()   # never set up: the first tap asks the OS (granted here) and records ON
        await b.until("localStorage.getItem('pc.concord.notify.v1.'+__PC.me().pubkey)==='on'")
        await tap_bell()   # on -> off
        await b.until("document.querySelector('#cc-notify').getAttribute('aria-pressed')==='false'")
        assert await b.js("document.querySelector('#cc-notify use').getAttribute('href')") == "#i-bell-off"
        assert await b.js("PCConcord.notificationsOn()") is False
        await tap_bell()
        await b.until("document.querySelector('#cc-notify').getAttribute('aria-pressed')==='true'")
        assert await b.js("PCConcord.notificationsOn()") is True

    asyncio.run(desktop.with_browser("online", "", check))
