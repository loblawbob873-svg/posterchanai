"""Clicking somebody's name in a window opens their profile in a NEW window; the window you were in stays.

The owner: "if you click on a username, have it open the profile in a new window instead of replacing
your view". On PosterChanOS every app is its own window and the desktop rule (renderProfileView →
PCOS.openDoc) is off inside it, so a name clicked in Social replaced the timeline in Social's own window.
Driven in the shipped bundle as a popped-out Social window with the desktop that opened it stubbed:

  * the avatar and the name both hand the profile to the desktop, and Social keeps showing Social;
  * the profile's own window still draws that profile in place (it is how its first paint arrives).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


BOB = "b" * 64
SETUP = r"""(()=>{
Store.saveEvent({id:'a'.repeat(64),kind:1,pubkey:'%s',created_at:Math.floor(Date.now()/1000)-5,tags:[],content:'hello from bob',sig:''});
window.__desk=[]; PCOSWin.desktop=()=>({PCOSWin:{enabled:()=>true}, PCOS:{isOn:()=>true}, Store:{saveEvent(){}},
  __PC:{openProfile:pk=>{ __desk.push(pk); }}});
return true;})()""" % BOB


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_name_in_a_window_opens_a_new_profile_window_and_the_window_stays():
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 1100, "height": 800, "deviceScaleFactor": 1, "mobile": False})
        await desktop.login(b)
        await b.until("!!(window.PCOSWin && PCOSWin.isWindow())")
        await b.js(SETUP)
        await b.js("__PC.switchView('global');true")
        await b.until("!!document.querySelector('#feed .note[data-pk=\"%s\"]')" % BOB)
        await b.js("document.querySelector('#feed .note[data-pk=\"%s\"] .av').click();true" % BOB)
        await asyncio.sleep(.4)
        got["avatar"] = await b.js("({desk:__desk.slice(), view:__PC.VIEW, still:!!document.querySelector('#feed .note[data-pk=\"%s\"]')})" % BOB)
        named = await b.js("!!document.querySelector('#feed .note[data-pk=\"%s\"] [data-prof]')" % BOB)
        if named:
            await b.js("document.querySelector('#feed .note[data-pk=\"%s\"] [data-prof]').click();true" % BOB)
            await asyncio.sleep(.4)
        got["named"] = named
        got["name"] = await b.js("({n:__desk.length, view:__PC.VIEW})")
        # Inside the profile's OWN window the profile draws in place.
        await b.js("PCOSWin.viewOf=()=>'doc:prof:%s'; __PC.openProfile('%s');true" % (BOB, BOB))
        await asyncio.sleep(.6)
        got["own"] = await b.js("({desk:__desk.length, view:__PC.VIEW})")

    asyncio.run(desktop.with_browser("online", "?pcwin=global", check,
                                     extra_init="window.pcShell.windowContext={role:'app',view:'global'};window.pcShell.backgroundOwner=false;"))
    a = got["avatar"]
    assert a["desk"] == [BOB], ("the profile was not handed to the desktop", a)
    assert a["view"] == "global" and a["still"], ("the window's own view was replaced", a)
    if got["named"]:
        assert got["name"] == {"n": 2, "view": "global"}, got["name"]
    n = got["name"]["n"]
    assert got["own"] == {"desk": n, "view": "profile"}, ("the profile's own window did not draw it", got["own"])
