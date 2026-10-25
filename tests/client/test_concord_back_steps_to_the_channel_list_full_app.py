"""Android Back inside Communities: chat -> channel list -> out, BY DEFAULT; opt-in, it never leaves.

Git issue "Back Arrow Customization Settings" (Vyram, 2026-10-24). Back from a channel left Communities for the
screen before it. The first answer put "chat -> channel list -> out" behind a switch; his reply: that should be the
DEFAULT, and the switch should do what he asked -- Back alternates chat < channel list < chat < channel list and
never leaves Communities. Also "looks weird in the settings": the switch had a bare <span>, not the slider every
other switch draws, so it rendered as an empty box. The shipped client at phone width, Back pressed through the
same handler Android calls.
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


SWITCH = "#set-concord-back-stays"


async def _open_room(b):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 850, "deviceScaleFactor": 1, "mobile": True})
    await desktop.login(b)
    await b.js("window.__publishOK=true")
    await b.js(ROOM)
    await b.until("!!document.querySelector('.cc-app')")
    await b.js("(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()")
    await b.until("!!document.querySelector('.cc-app.show-chat')")


async def _back(b):
    """One press of Android's Back, as app.js's backButton handler decides it: did Communities take the step?"""
    took = await b.js("__PC.concordBack()")
    await asyncio.sleep(.4)
    shown = await b.js("({chat:!!document.querySelector('.cc-app.show-chat'),list:[...document.querySelectorAll('.cc-channel')].some(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0&&r.left>=0&&r.right<=innerWidth+1;}),"
                       "concord:document.body.classList.contains('concord-view')})")
    return took, shown


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_by_default_back_goes_from_a_channel_to_the_channel_list_and_then_out():
    got = {}

    async def check(b):
        await _open_room(b)
        got["default"] = await b.js("ClientSettings.get('concordBackStaysIn',false)")
        got["one"] = await _back(b)
        got["two"] = await _back(b)

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    assert got["default"] is False, "staying in Communities must be opt-in"
    assert got["one"] == (True, {"chat": False, "list": True, "concord": True}), \
        ("by default, Back from a channel must show the channel list", got)
    assert got["two"][0] is False, ("at the channel list Back is the app's (leaves Communities)", got)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_with_the_switch_on_back_alternates_chat_and_channel_list_and_never_leaves():
    got = {}

    async def check(b):
        await _open_room(b)
        await b.js("__PC.switchView('settings')")
        await b.until("!!document.querySelector('%s')" % SWITCH)
        got["slider"] = await b.js("(()=>{const s=document.querySelector('%s').nextElementSibling;const r=s&&s.getBoundingClientRect();"
                                   "return {slider:!!s&&s.classList.contains('slider'),w:r?Math.round(r.width):0,h:r?Math.round(r.height):0};})()" % SWITCH)
        await b.js("(()=>{const c=document.querySelector('%s');c.checked=true;c.dispatchEvent(new Event('change',{bubbles:true}));})()" % SWITCH)
        await b.js("__PC.switchView('concord')")
        await b.until("!!document.querySelector('.cc-app')")
        await b.js("(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()")
        await b.until("!!document.querySelector('.cc-app.show-chat')")
        got["presses"] = [await _back(b) for _ in range(4)]
        await b.js("location.reload()")
        await b.until("!!window.ClientSettings")
        got["after_reload"] = await b.js("ClientSettings.get('concordBackStaysIn',false)")

    asyncio.run(desktop.with_browser("online", "", check, CLASSIC))
    assert got["slider"]["slider"] and got["slider"]["w"] > 20 and got["slider"]["h"] > 10, \
        ("the switch does not draw a slider (it 'looks weird')", got["slider"])
    assert got["after_reload"] is True, ("the switch did not stick", got)
    list_, chat = {"chat": False, "list": True, "concord": True}, {"chat": True, "list": False, "concord": True}
    assert [p[1] for p in got["presses"]] == [list_, chat, list_, chat], ("Back must alternate chat < list < chat < list", got)
    assert all(p[0] is True for p in got["presses"]), ("Back left Communities with the switch on", got)


def test_the_android_back_handler_asks_communities_before_walking_history():
    app = (ROOT / "static/js/client/app.js").read_text()
    h = app[app.index("_App.addListener('backButton'"):]
    step, walk = h.index("concordBack()"), h.index("history.back()")
    assert step < walk, "Communities must get its step before the app walks its history"
