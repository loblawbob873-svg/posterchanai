"""Concord keeps a room at its newest message through the two things that pulled it away on the laptop (2026-10-29).

"the scroll is not at the latest message" -- measured live: #general 836px under the fold, saved state pinned:false,
nobody had scrolled; the desktop had RESIZED the window at startup (1127 -> 806px tall). And "pressing enter on laptop
after message caused the scroll to go up too". Both are the room being moved by something that is not the reader;
both are driven here the way they happened -- the viewport really shrinks, the keyboard really presses Enter -- and the
room is sampled after each, for long enough that a late correction cannot hide a miss and a late pull cannot slip by.

GUARDS, NOT REPRODUCTIONS: both pass on the code from before the fix too (measured). Neither a viewport shrink, an
in-page window shrunk by Arrange, nor Enter moved the room in Chrome; what unpinned the laptop's room was a scroll
nobody made, judged by distance. THAT is reproduced -- and fails on the old code -- by
test_concord_room_stays_at_newest_full_app.py::test_only_the_reader_can_unpin_a_room. These keep the two reported
paths covered as the code moves.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_scroll_never_shows_the_top_full_app import SETUP, settle

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")

AT = """(()=>{const m=document.querySelector('.cc-messages');
  return {fromBottom:Math.round(m.scrollHeight-m.clientHeight-m.scrollTop), top:Math.round(m.scrollTop), h:m.clientHeight};})()"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _open_room(b, width, height):
    await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=height, deviceScaleFactor=1, mobile=width < 600))
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js(SETUP)
    await b.until("document.querySelectorAll('.cc-message').length>=50 || !!document.querySelector('[data-cc-channel]')")
    await b.js("(()=>{const c=document.querySelector('[data-cc-channel]');if(c&&!document.querySelector('.cc-app.show-chat .cc-message'))c.click();})()")
    await b.until("document.querySelectorAll('.cc-message').length>=50")
    await settle(b, 2)


async def _trail(b, n=20):
    out = []
    for _ in range(n):              # 2 s of samples: every one must be at the bottom, not just the last
        await asyncio.sleep(0.1)
        out.append(await b.js(AT))
    return out


@CHROME
@pytest.mark.parametrize("width", [1280, 390])
def test_the_window_resizing_under_an_open_room_keeps_it_at_the_newest_message(width):
    got = {}

    async def check(b):
        await _open_room(b, width, 850)
        got["entered"] = await b.js(AT)
        # The desktop tiles its windows at startup: the room's window gets much shorter, then a little taller.
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=600, deviceScaleFactor=1, mobile=width < 600))
        got["shrunk"] = await _trail(b)
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=720, deviceScaleFactor=1, mobile=width < 600))
        got["grew"] = await _trail(b)
        # ...and the room still follows its newest message afterwards: one arrives.
        await b.js("__msgs.push({id:'n'+'z'.repeat(63),pubkey:'b'.repeat(64),text:'after the resize',at:6000,kind:9,tags:[]});PCConcord.refreshActiveChannel&&PCConcord.refreshActiveChannel()")
        await b.until("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('after the resize'))")
        got["arrived"] = await _trail(b)

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["entered"]["fromBottom"] <= 2, got["entered"]
    assert got["shrunk"][-1]["h"] < got["entered"]["h"], ("the viewport did not get shorter, so this proves nothing", got["shrunk"][-1])
    for what in ("shrunk", "grew", "arrived"):
        worst = max(s["fromBottom"] for s in got[what][3:])     # 0.3 s to settle, then never off the bottom
        assert worst <= 2, (f"after the window {what}, the newest message was {worst}px under the fold", got[what])


@CHROME
@pytest.mark.parametrize("width", [1280, 390])
def test_sending_with_enter_keeps_the_room_at_the_newest_message(width):
    got = {}

    async def check(b):
        await _open_room(b, width, 850)
        got["entered"] = await b.js(AT)
        await b.js("document.getElementById('cc-input').focus()")
        for n in range(3):                                      # three messages in a row, as people send them
            await b.call("Input.insertText", {"text": f"message {n} sent with enter"})
            for t in ("rawKeyDown", "char", "keyUp"):
                ev = {"type": t, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}
                if t == "char":
                    ev["text"] = "\r"
                await b.call("Input.dispatchKeyEvent", ev)
            await b.until(f"[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('message {n} sent with enter'))")
            got[f"sent{n}"] = await _trail(b, 15)
        got["composer"] = await b.js("document.getElementById('cc-input').value")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["entered"]["fromBottom"] <= 2, got["entered"]
    assert got["composer"] == "", ("Enter did not send", got["composer"])
    for n in range(3):
        worst = max(s["fromBottom"] for s in got[f"sent{n}"])
        assert worst <= 2, (f"after sending message {n} with Enter, the newest message was {worst}px under the fold", got[f"sent{n}"])
