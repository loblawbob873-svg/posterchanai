"""Concord: the channel's topic can be read in full ("unable to see Room topic message, you see some text at the top of
the channel but its cut off. need a way to view it better", 2026-10-26).

Measured before: at 1280px the topic was drawn in 291px of a 964px line, the rest behind an ellipsis; on a phone it was
not shown at all. Now the topic itself opens the full text on a wide screen, and an ⓘ button does on a phone (there
is no room for the line). The sheet holds every word, line breaks kept, inside the screen.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_react_full_app import ROOM

CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')"
           "||'{}'),osMode:false}));")
TOPIC = ("Welcome to the PosterChan community!\nRules: be kind, no spam, share your projects in #projects, report bugs "
         "in #git, and have fun building the future of Nostr together.")
SHEET = r"""(()=>{const s=document.querySelector('.cc-topic-full');if(!s)return null;const r=s.getBoundingClientRect();
return {text:s.innerText,inside:r.left>=-1&&r.right<=innerWidth+1&&r.width>100};})()"""


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _channel(b, width):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 850, "deviceScaleFactor": 1, "mobile": width < 600})
    await desktop.login(b)
    await b.js("window.__publishOK=true")
    await b.js(ROOM.replace("name:'React fixture',", "name:'React fixture',description:%s," % json.dumps(TOPIC)))
    await b.until("!!document.querySelector('.cc-app')")
    await b.js("(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()")
    await b.until("!!document.querySelector('.cc-conversation>header')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_on_a_phone_the_info_button_opens_the_whole_topic():
    got = {}

    async def check(b):
        await _channel(b, 390)
        await b.until("!!document.querySelector('#cc-topic-open')")
        got["button"] = await b.js("(()=>{const r=document.querySelector('#cc-topic-open').getBoundingClientRect();return r.width>=30&&r.right<=innerWidth+1})()")
        await b.js("document.querySelector('#cc-topic-open').click()")
        await b.until("!!document.querySelector('.cc-topic-full')")
        got["sheet"] = await b.js(SHEET)

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    assert got["button"], ("the ⓘ button is missing or off the screen", got)
    assert got["sheet"] and got["sheet"]["inside"], got
    assert got["sheet"]["text"].replace("\n", " ").split() == TOPIC.replace("\n", " ").split(), ("the topic is not all there", got)
    assert "\n" in got["sheet"]["text"], ("line breaks were lost", got)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_on_a_wide_screen_the_cut_off_topic_opens_in_full():
    got = {}

    async def check(b):
        await _channel(b, 1280)
        got["cut"] = await b.js("(()=>{const t=document.querySelector('.cc-conversation>header .cc-topic');return t.scrollWidth>t.clientWidth})()")
        await b.js("document.querySelector('.cc-conversation>header .cc-topic').click()")
        await b.until("!!document.querySelector('.cc-topic-full')")
        got["sheet"] = await b.js(SHEET)

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    assert got["cut"], ("the fixture topic should overflow the header", got)
    assert got["sheet"] and got["sheet"]["text"].replace("\n", " ").split() == TOPIC.replace("\n", " ").split(), got
