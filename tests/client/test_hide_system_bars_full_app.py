"""Settings -> Phone -> "Hide system bars": the APK's full-screen switch.

Asked for on a Samsung tablet that showed One UI's taskbar AND PosterChan's own dock ("any way to hide
OS dock?"). The switch drives HomeScreen.setSystemBarsHidden (SystemBars.java: immersive mode, stored
per device, re-applied on resume and focus). Here: the row reads the stored state, a change reaches the
phone, a refused change is put back, and an APK without the call gets no switch that does nothing.
The window itself is measured on a real emulator by SystemBarsDeviceTest.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def native(stored, with_call=True, refuse=False):
    call = ("setSystemBarsHidden:async o=>{window.__barCalls.push(o);%s return {hidden:o.hidden};}," %
            ("throw new Error('nope');" if refuse else "")) if with_call else ""
    return r'''
window.__barCalls=[];
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=> 'android',Plugins:{HomeScreen:{
 consumeLaunchView:async()=>({view:''}), addListener:()=>({remove(){}}), formFactor:async()=>({form:'phone'}),
 %s systemBars:async()=>({hidden:%s})
}}};''' % (call, 'true' if stored else 'false')


async def _phone_pane(b):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": 412, "height": 900, "deviceScaleFactor": 2, "mobile": True})
    await desktop.login(b)
    await b.until("!!window.__PC_BOOTED")
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js("__PC.switchView('settings')")
    await b.until("!!document.querySelector('.us-tab[data-tab=phone]')")
    await b.js("document.querySelector('.us-tab[data-tab=phone]').click()")
    await asyncio.sleep(.6)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_switch_reads_the_phone_and_a_change_reaches_it():
    got = {}

    async def check(b):
        await _phone_pane(b)
        got["visible"] = await b.js("(()=>{const r=document.querySelector('#set-hide-bars');if(!r)return null;const x=r.closest('label.fld').getBoundingClientRect();return x.width>0&&x.right<=innerWidth+1})()")
        got["initial"] = await b.js("document.querySelector('#set-hide-bars').checked")
        await b.js("(()=>{const s=document.querySelector('#set-hide-bars');s.checked=true;s.dispatchEvent(new Event('change'))})()")
        await asyncio.sleep(.4)
        got["calls"] = await b.js("__barCalls")
        got["after"] = await b.js("document.querySelector('#set-hide-bars').checked")

    asyncio.run(desktop.with_browser("online", "", check, native(False)))
    assert got["visible"] is True and got["initial"] is False, got
    assert got["calls"] == [{"hidden": True}] and got["after"] is True, got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_stored_choice_shows_as_on_and_a_refusal_is_put_back():
    got = {}

    async def check(b):
        await _phone_pane(b)
        got["initial"] = await b.js("document.querySelector('#set-hide-bars').checked")
        await b.js("(()=>{const s=document.querySelector('#set-hide-bars');s.checked=false;s.dispatchEvent(new Event('change'))})()")
        await asyncio.sleep(.4)
        got["after"] = await b.js("document.querySelector('#set-hide-bars').checked")

    asyncio.run(desktop.with_browser("online", "", check, native(True, refuse=True)))
    assert got["initial"] is True and got["after"] is True, got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_apk_without_the_call_shows_no_switch():
    got = {}

    async def check(b):
        await _phone_pane(b)
        got["row"] = await b.js("!!document.querySelector('#us-bars-row')")

    asyncio.run(desktop.with_browser("online", "", check, native(False, with_call=False)))
    assert got["row"] is False, got


def test_the_activity_re_applies_it_whenever_android_may_have_dropped_it():
    """Immersive mode is dropped by dialogs, permission prompts and app switches."""
    root = Path(__file__).resolve().parents[2] / "mobile/android/app/src/main/java/place/poster/app"
    main = (root / "MainActivity.java").read_text()
    resume = main[main.index("public void onResume()"):][:400]
    focus = main[main.index("public void onWindowFocusChanged("):][:300]
    assert "SystemBars.apply(this)" in resume and "if (hasFocus) SystemBars.apply(this)" in focus
    bars = (root / "SystemBars.java").read_text()
    assert "BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE" in bars and "Type.systemBars()" in bars
