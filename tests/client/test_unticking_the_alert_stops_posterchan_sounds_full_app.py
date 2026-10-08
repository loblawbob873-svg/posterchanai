"""Unticking "PosterChan notifications play it" means no PosterChan sound -- not a different PosterChan sound.

Reported: "can we make the posterchan notification sounds optional? one user complained … he said he unchecked it
but still played". Unticking the Alert put back the sound from BEFORE it -- and for nearly everybody that was the
default, the PosterChan cyberpunk chime (since deploy 135 also the Android default). So the person who switched
PosterChan's sound off heard PosterChan's other sound. Unticking now lands on "System sound": the phone's own
notification sound on Android, and nothing of ours on the web and desktop. A sound somebody explicitly picked
before (soft, bright, silent) is still what comes back. "System sound" is also offered in Notifications.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_ringtone_settings_full_app import _open_tab
from tests.client.test_posterchan_alert_sound_full_app import ANDROID, AUDIO_PROBE

@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
ANDROID_SOUNDS = ANDROID.replace("__appAlert.push({on:!!o.on,test:!!o.test})", "__appAlert.push({on:!!o.on,test:!!o.test,sound:o.sound})")


@CHROME
def test_on_the_web_unticking_the_alert_plays_nothing_of_ours():
    got = {}

    async def check(b):
        await _open_tab(b)
        await b.js(AUDIO_PROBE)
        await b.js("document.getElementById('al-app').click();true")
        await b.until("/^✓ On/.test(document.getElementById('al-said').textContent)")
        await b.js("document.getElementById('al-app').click();true")
        await b.until("/^Off/.test(document.getElementById('al-said').textContent)")
        got["pref"] = await b.js("__PC.notificationPreference('sound')")
        got["said"] = await b.js("document.getElementById('al-said').textContent")
        await asyncio.sleep(.6)
        await b.js("window.__played.length=0; __PC.notificationSound(); true")
        await asyncio.sleep(.4)
        got["played"] = await b.js("__played.slice()")
        got["options"] = await b.js("(()=>{const s=document.getElementById('us-notification-sound');return s?[...s.options].map(o=>o.value):null})()")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["pref"] == "system", ("unticking left a PosterChan sound on", got)
    assert not any("posterchan" in p for p in got["played"]), ("a PosterChan sound still played", got)
    assert "no PosterChan sound" in got["said"], ("the line does not say what unticking did", got["said"])
    assert got["options"] is None or "system" in got["options"], got


@CHROME
def test_on_android_unticking_puts_the_phone_on_its_own_sound():
    got = {}

    async def check(b):
        await _open_tab(b)
        await b.js("document.getElementById('al-app').click();true")
        await b.until("__appAlert.length>0")
        await b.js("window.__appAlert.length=0; document.getElementById('al-app').click();true")
        await b.until("__appAlert.length>0")
        got["calls"] = await b.js("__appAlert")

    asyncio.run(desktop.with_browser("online", "", check, ANDROID_SOUNDS))
    assert got["calls"][0]["sound"] == "system" and got["calls"][0]["on"] is False, got


@CHROME
def test_a_sound_somebody_picked_before_the_alert_is_what_comes_back():
    got = {}

    async def check(b):
        await _open_tab(b)
        await b.js("__PC.setNotificationPreference('sound','soft');true")
        await asyncio.sleep(.3)
        await b.js("document.getElementById('al-app').click();true")
        await b.until("__PC.notificationPreference('sound')==='posterchan'")
        await asyncio.sleep(.3)
        await b.js("document.getElementById('al-app').click();true")
        await b.until("__PC.notificationPreference('sound')!=='posterchan'")
        got["pref"] = await b.js("__PC.notificationPreference('sound')")

    asyncio.run(desktop.with_browser("online", "", check))
    assert got["pref"] == "soft", got
