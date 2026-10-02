"""Telegram is on a tablet and off a phone -- decided by the device, not by the screen's size.

Reported: "telegram is missing from tablet. the deal was for it not to work on phone but work on android
tablet" (a Samsung tablet). isPhone() guessed from the screen's short side in CSS px (< 480), and One UI's
Screen zoom makes an 800px-wide tablet report ~420-450 -- phone-sized. The APK now answers from the
device's own configuration (HomeScreen.formFactor: TelephonyManager.isVoiceCapable), and the page
re-gates when it does. With no answer (a browser, an older APK) the size rule is unchanged.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_telegram_client_full_app import FAKE


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def native(form):
    return r'''
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=> 'android',Plugins:{HomeScreen:{
 consumeLaunchView:async()=>({view:''}), addListener:()=>({remove(){}}),
 formFactor:async()=>({form:%r,voiceCapable:%s,swDp:420})
}}};''' % (form, 'true' if form == 'phone' else 'false')


NOTICE = "(document.querySelector('#feed')||{}).textContent.includes('Telegram lives on your phone')"


async def _tg(b, w, h):
    await b.call("Emulation.setDeviceMetricsOverride", {"width": w, "height": h, "deviceScaleFactor": 2, "mobile": True,
                                                         "screenWidth": w, "screenHeight": h})
    await desktop.login(b)
    await b.until("!!window.__PC_BOOTED")
    await asyncio.sleep(1)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js("__PC.switchView('tg')")
    await asyncio.sleep(1.2)
    return {"hidden": await b.js("document.documentElement.classList.contains('pc-no-tg')"),
            "notice": await b.js(NOTICE),
            "client": await b.js("!!document.querySelector('#feed .tg-app:not(.tg-empty)')")}


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_tablet_with_screen_zoom_gets_telegram():
    got = {}

    async def check(b):
        got.update(await _tg(b, 420, 900))     # a zoomed Samsung tablet's short side

    asyncio.run(desktop.with_browser("online", "", check, FAKE + native("tablet")))
    assert got == {"hidden": False, "notice": False, "client": True}, got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_phone_never_gets_telegram_however_wide():
    got = {}

    async def check(b):
        got.update(await _tg(b, 700, 1000))    # a big phone / an unfolded foldable

    asyncio.run(desktop.with_browser("online", "", check, FAKE + native("phone")))
    assert got["hidden"] is True and got["notice"] is True, got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_without_an_answer_the_size_rule_still_applies():
    got = {}

    async def check(b):
        got["small"] = await _tg(b, 400, 850)

    asyncio.run(desktop.with_browser("online", "", check, FAKE))
    assert got["small"]["hidden"] is True and got["small"]["notice"] is True, got


def test_the_phone_rule_is_the_physical_screen_with_the_voice_radio_as_tiebreak():
    """It used to be the voice radio alone, which called an LTE Samsung Galaxy Tab a phone ("I need
    telegram to be available on android tablets"). Size decides; the radio settles the 6.5-7.5" band
    and an unknown size. The per-device table is test_telegram_shows_on_tablets.py."""
    import shutil, subprocess, tempfile, os
    javac, java = shutil.which("javac"), shutil.which("java")
    if not javac or not java:
        pytest.skip("no JDK")
    src = Path(__file__).resolve().parents[2] / "mobile/android/app/src/main/java/place/poster/app/home/FormFactor.java"
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "place/poster/app/home"; pkg.mkdir(parents=True)
        (pkg / "FormFactor.java").write_text(src.read_text())
        (Path(tmp) / "H.java").write_text("public class H{public static void main(String[] a){"
            "System.out.println(place.poster.app.home.FormFactor.classify(true)+\" \"+place.poster.app.home.FormFactor.classify(false)"
            "+\" \"+place.poster.app.home.FormFactor.classify(true, 11.0));}}")
        r = subprocess.run([javac, "-d", tmp, str(pkg / "FormFactor.java"), str(Path(tmp) / "H.java")], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        r = subprocess.run([java, "-cp", tmp, "H"], capture_output=True, text=True)
    assert r.stdout.split() == ["phone", "tablet", "tablet"], r.stdout
    plugin = (src.parent / "HomePlugin.java").read_text()
    start = plugin.index("public void formFactor(")
    method = plugin[start:plugin.index("call.resolve(o);", start)]
    assert "isVoiceCapable()" in method and "getRealMetrics" in method and "FormFactor.classify(voice, inches)" in method
