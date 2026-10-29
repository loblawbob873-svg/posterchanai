"""Jumping to a pinned message stays on it.

Only the reader can unpin a room now (a scroll nobody made snaps a room back to its newest message -- "the scroll
is not at the latest message", 2026-10-29). The app's own jumps to an older message must therefore unpin it
FIRST, as jump-to-reply and jump-to-linked-message already did; the 📌 list's jump did not, and was pulled straight
back to the bottom. Real clicks: ☰ → Pinned messages, then the pinned message.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP

# The scroll fixture's room, with one pinned message (#12, far above the newest) -- the reader's pin list is
# read when the room is drawn, so it is part of the fixture rather than added afterwards.
PINNED_SETUP = SETUP.replace(
    "window.PosterCordReader={",
    "window.PosterCordReader={inspectPinList:()=>({available:true,entries:[{id:__msgs[12].id,pubkey:__msgs[12].pubkey,content:__msgs[12].text}]}),")


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def click(b, sel):
    r = await b.js("(()=>{const e=document.querySelector(%r),q=e.getBoundingClientRect();return [q.x+q.width/2,q.y+q.height/2]})()" % sel)
    for t in ("mousePressed", "mouseReleased"):
        await b.call("Input.dispatchMouseEvent", {"type": t, "button": "left", "clickCount": 1, "x": r[0], "y": r[1]})


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_jumping_to_a_pinned_message_stays_on_it():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=1280, height=850, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js(PINNED_SETUP)
        await b.until("document.querySelectorAll('.cc-message').length>=60")
        await asyncio.sleep(1)
        await click(b, "#cc-head-more")
        await b.until("""!!document.querySelector('.menu-pop [data-m="cc-pins"]')""")
        await click(b, '.menu-pop [data-m="cc-pins"]')
        await b.until("!!document.querySelector('[data-cc-pin-jump]')")
        await click(b, "[data-cc-pin-jump]")
        await asyncio.sleep(1.5)
        got["row"] = await b.js("""(()=>{const r=document.querySelector('.cc-message[data-message-id^="m012"]').getBoundingClientRect(),
          q=document.querySelector('.cc-messages').getBoundingClientRect();return {inView:r.bottom>q.top&&r.top<q.bottom,
          fromBottom:Math.round(document.querySelector('.cc-messages').scrollHeight-document.querySelector('.cc-messages').clientHeight-document.querySelector('.cc-messages').scrollTop)};})()""")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["row"]["inView"], ("jumping to a pinned message was pulled back to the newest message", got)
