"""On a phone, an open Concord channel keeps the app's bottom nav ("you lose the navbar on the bottom when using
concord on android", 2026-10-25).

The channel list stopped above the nav (bottom inset 61px + safe area), but opening a channel made `.cc-app.show-chat`
a full-screen layer (`inset:0`, z-index 45 over the nav's 40, height 100dvh) -- the composer textarea sat exactly on
the nav, and the only way to another screen was Back. The shipped client at phone width: the nav is the topmost
element where it is drawn, and the composer ends above it.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_react_full_app import ROOM

CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')"
           "||'{}'),osMode:false}));")

PROBE = """(()=>{const n=document.querySelector('.mobilenav'),c=document.querySelector('.cc-app.show-chat textarea');
const r=n.getBoundingClientRect(),cs=getComputedStyle(n),pts=[0.15,0.5,0.85].map(f=>{
  const t=document.elementFromPoint(Math.round(r.left+r.width*f),Math.round(r.top+r.height/2));return !!(t&&t.closest('.mobilenav'));});
const cr=c?c.getBoundingClientRect():null;
return {shown:cs.display!=='none'&&cs.visibility!=='hidden'&&r.height>40,navOnTop:pts.every(Boolean),
        navTop:Math.round(r.top),composerBottom:cr?Math.round(cr.bottom):null,composerH:cr?Math.round(cr.height):0};})()"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width,height", [(390, 850), (360, 640)])
def test_an_open_channel_keeps_the_bottom_nav(width, height):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": True})
        await desktop.login(b)
        await b.js("window.__publishOK=true")
        await b.js(ROOM)
        await b.until("!!document.querySelector('.cc-app')")
        got["list"] = await b.js(PROBE)
        await b.js("(()=>{const ch=document.querySelector('.cc-channel');if(ch)ch.click();})()")
        await b.until("!!document.querySelector('.cc-app.show-chat textarea')")
        await asyncio.sleep(.4)
        got["chat"] = await b.js(PROBE)

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    assert got["list"]["shown"] and got["list"]["navOnTop"], ("the channel list lost the nav", got)
    chat = got["chat"]
    assert chat["shown"] and chat["navOnTop"], ("an open channel covers the bottom nav", got)
    assert chat["composerH"] > 20 and chat["composerBottom"] <= chat["navTop"] + 1, ("the composer runs under the nav", got)
