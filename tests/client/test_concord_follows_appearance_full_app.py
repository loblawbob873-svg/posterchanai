"""Concord's own colours follow Settings -> Appearance ("The accent color is only applying to the write a note button",
Vyram, git issue "Concord Theme match with app theme", 2026-10-26, screenshots).

With Accent yellow and the Communities section accent green in Appearance, Communities drew "TEXT CHANNELS" in the
preset's magenta and the selected channel's bar in its cyan -- Concord painted only from its OWN theme picker, so
Appearance reached the Write-a-note button and nothing Concord draws. The rule now, most specific first: the
Communities section colour, else Concord's own picker (older choices), else the app accent, else the preset; and
Concord's picker writes the Communities section, so the two are one setting. The real client at phone width.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_react_full_app import ROOM

NO_OS = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')"
         "||'{}'),osMode:false}));")
PROBE = r"""(()=>{const sec=document.querySelector('.cc-section'),ch=document.querySelector('.cc-channel.active');
return {section:sec&&getComputedStyle(sec).color,bar:ch&&getComputedStyle(ch).boxShadow,
        accent:getComputedStyle(document.documentElement).getPropertyValue('--cct-accent').trim()};})()"""
GREEN, YELLOW = "rgb(0, 255, 0)", "rgb(255, 255, 0)"


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _concord(b):
    await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 850, 'deviceScaleFactor': 1, 'mobile': True})
    await desktop.login(b)
    await b.js("window.__publishOK=true")
    await b.js(ROOM)
    await b.until("!!document.querySelector('.cc-app') && !!document.querySelector('.cc-section')")


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_concord_takes_the_communities_colour_then_the_app_accent():
    got = {}

    async def check(b):
        await _concord(b)
        got["preset"] = await b.js(PROBE)
        await b.js("PCLook.set({accent:'#ffff00'});true")
        await asyncio.sleep(.4)
        got["app"] = await b.js(PROBE)
        await b.js("PCLook.setSection('concord',{accent:'#00ff00'});true")
        await asyncio.sleep(.4)
        got["section"] = await b.js(PROBE)
        # An older choice in Concord's own picker does not outrank the Communities colour set in Appearance.
        await b.js("PCConcordTheme.set({accent:'#ff0000'},{save:false});PCLook.setSection('concord',{accent:'#00ff00'});true")
        await asyncio.sleep(.4)
        got["both"] = await b.js(PROBE)
        # Concord's own picker writes the Communities colour: one setting, not two.
        await b.js("PCConcordTheme.openPicker(__PC)")
        await b.until("!!document.querySelector('#cc-theme-accent')")
        await b.js("(()=>{const i=document.querySelector('#cc-theme-accent');i.value='#3366ff';"
                   "i.dispatchEvent(new Event('change',{bubbles:true}))})()")
        await asyncio.sleep(.4)
        got["picker_wrote"] = await b.js("(PCLook.get().sec.concord||{}).accent||''")

    asyncio.run(desktop.with_browser("online", "", check, NO_OS))
    assert got["preset"]["section"] not in (GREEN, YELLOW), json.dumps(got)
    assert got["app"]["section"] == YELLOW and YELLOW in got["app"]["bar"], ("Concord ignores the app accent", json.dumps(got))
    assert got["section"]["section"] == GREEN and GREEN in got["section"]["bar"], ("Concord ignores the Communities colour", json.dumps(got))
    assert got["both"]["section"] == GREEN, ("an old Concord picker colour outranked Appearance", json.dumps(got))
    assert got["picker_wrote"] == "#3366ff", ("Concord's picker and Appearance are two different settings", json.dumps(got))
