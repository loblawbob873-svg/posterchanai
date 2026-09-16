"""A launcher tile on a tablet opens in the SAME mode as the app itself — desktop or classic.

Reported on the Android APK: "it seems to get confused whether it should open launcher apps in classic
or desktop". Measured against the shipped bundle, it had two answers on one device held one way:
the app's own icon restored the remembered desktop (`PCOS.restore`), every launcher tile left it
(`mobileLanding` exited unconditionally), and a tablet booted upright never got its desktop back when
turned sideways while one booted sideways kept it through every rotation.

The rule this pins is one function, `wantsDesktop`: a desktop already on screen stays; otherwise the
remembered `osMode` decides whenever the screen is wide enough to hold one. The matrix is every
carrier a tile has (cold = parked before the page loads, warm = the native `launchView` event), the
app icon with no tile, both orientations, both preferences, and a rotation — each asserted against
the same expected mode, because the bug was never one wrong answer but two different ones.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# The Android shell, not the Electron one: no pcShell/pcPopup, a Capacitor HomeScreen plugin that
# hands over a parked tile exactly once. The harness forces osMode on for Electron; undo that per test.
ANDROID = r'''
delete window.pcShell; delete window.pcPopup;
{const s=JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}');
 const p=localStorage.getItem('__osPref'); if(p!==null){s.osMode=p==='1';}
 localStorage.setItem('pc_nostr_settings',JSON.stringify(s));}
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=> 'android',Plugins:{HomeScreen:{
 consumeLaunchView:async()=>{const view=localStorage.getItem('__park')||'';localStorage.removeItem('__park');return {view};},
 addListener:(name,fn)=>{if(name==='launchView')window.__launchListener=fn;return {remove(){}};}
}}};
'''

STATE = ("({on:!!(window.PCOS&&PCOS.isOn()),view:__PC.VIEW,"
         "wins:(PCOS.windows?PCOS.windows().map(w=>w.view):[]),"
         "pref:JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}').osMode})")

SIZES = {'landscape': (1280, 800), 'portrait': (800, 1280)}


async def metrics(b, w, h):
    await b.call('Emulation.setDeviceMetricsOverride', {'width': w, 'height': h, 'deviceScaleFactor': 2, 'mobile': True})
    await b.call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})


async def settled(b):
    # A wrong mode is often a LATE flip (a restore after the landing), so look after things settle.
    await asyncio.sleep(1.2)
    return await b.js(STATE)


def expect(state, desktop_on, view, what):
    assert state['on'] is desktop_on, (what, state)
    assert state['view'] == view, (what, state)
    if desktop_on:
        assert view in state['wins'], ('the tile did not open as a window on the desktop', what, state)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('pref', ['1', '0'])
@pytest.mark.parametrize('orientation', ['landscape', 'portrait'])
def test_tile_icon_and_rotation_agree_on_the_mode(orientation, pref):
    w, h = SIZES[orientation]
    wide = orientation == 'landscape'
    want = pref == '1' and wide

    async def check(b):
        await desktop.login(b)
        await metrics(b, w, h)
        await b.js(f"localStorage.setItem('__osPref','{pref}');localStorage.setItem('__park','notes')")
        await b.call('Page.reload')
        await asyncio.sleep(.3)
        await b.until('!!window.__PC_BOOTED && !!window.__launchListener')
        expect(await settled(b), want, 'notes', 'cold tile')

        await b.js("__launchListener({view:'budget'})")
        expect(await settled(b), want, 'budget', 'warm tile')

        # The app's own icon: no tile at all. It must agree with the tiles above.
        await b.call('Page.reload')
        await asyncio.sleep(.3)
        await b.until('!!window.__PC_BOOTED && !!window.__launchListener')
        icon = await settled(b)
        assert icon['on'] is want, ('the app icon and the launcher tiles disagree', icon)

        await b.js("__launchListener({view:'notes'})")
        expect(await settled(b), want, 'notes', 'warm tile after the icon')

        # Turn the tablet. Sideways with the desktop remembered is a desktop, carrying the open
        # screen into a window; upright never tears one down.
        await metrics(b, h, w)
        rotated = await settled(b)
        expect(rotated, pref == '1', 'notes', 'rotated')
        assert rotated['pref'] is (pref == '1'), ('a launch or rotation rewrote the preference', rotated)

    asyncio.run(desktop.with_browser('online', '', check, ANDROID))
