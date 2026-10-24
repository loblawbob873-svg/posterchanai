"""Android Back inside Communities can step back to the channel list (opt-in).

Git issue "Back Arrow Customization Settings" (Vyram, 2026-10-24): Back from a channel left Communities for the
screen before it; he asked for a setting so Back goes channel -> channel list first. The shipped client at phone
width: with the switch on, Back from an open channel shows the channel list, and at the list Back is the app's again;
the switch is saved on the device; the Android back handler asks Communities before walking the app's history.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_react_full_app import ROOM

ROOT = Path(__file__).resolve().parents[2]
CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')"
           "||'{}'),osMode:false}));")


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_back_goes_from_a_channel_to_the_channel_list_and_then_out():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 850, "deviceScaleFactor": 1, "mobile": True})
        await desktop.login(b)
        await b.js("window.__publishOK=true")
        await b.js(ROOM)
        await b.until("!!document.querySelector('.cc-app')")
        await b.js("(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()")
        await b.until("!!document.querySelector('.cc-app.show-chat')")
        got["in_channel"] = await b.js("PCConcord.backStep()")
        await asyncio.sleep(.4)
        got["after_one"] = await b.js("({chat:!!document.querySelector('.cc-app.show-chat'),list:!!document.querySelector('.cc-channel')})")
        got["at_list"] = await b.js("PCConcord.backStep()")
        # The switch: off by default, saved when turned on, still on after a reload.
        got["default"] = await b.js("ClientSettings.get('concordBackToList',false)")
        await b.js("__PC.switchView('settings')")
        await b.until("!!document.querySelector('#set-concord-back')")
        await b.js("(()=>{const c=document.querySelector('#set-concord-back');c.checked=true;c.dispatchEvent(new Event('change',{bubbles:true}));})()")
        await b.js("location.reload()")
        await b.until("!!window.ClientSettings")
        got["after_reload"] = await b.js("ClientSettings.get('concordBackToList',false)")

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    assert got["in_channel"] is True, got
    assert got["after_one"] == {"chat": False, "list": True}, ("Back from a channel did not show the channel list", got)
    assert got["at_list"] is False, ("at the channel list Back must be the app's (leave Communities)", got)
    assert got["default"] is False, "the setting must be opt-in"
    assert got["after_reload"] is True, ("the switch did not stick", got)


def test_the_android_back_handler_asks_communities_before_walking_history():
    app = (ROOT / "static/js/client/app.js").read_text()
    h = app[app.index("_App.addListener('backButton'"):]
    step, walk = h.index("PCConcord.backStep()"), h.index("history.back()")
    assert step < walk, "Communities must get its step before the app walks its history"
    assert "ClientSettings.get('concordBackToList', false)" in h[:walk], "only with the switch on"
