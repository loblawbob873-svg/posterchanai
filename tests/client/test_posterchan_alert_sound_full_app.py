"""The PosterChan Alert, in the shipped client: one switch turns it on, and notifications then PLAY it.

"can you make a alert or notification sound for posterchan launcher that user can easily enable?" -- "cute anime
cyber punk sound". Driven in the real bundle:
  * User Settings → Sounds has the alert first, a ▶ that plays the bundled file, and ONE switch;
  * the switch IS the account's "App arrival sound" preference (set to 'posterchan'), so Notifications shows the
    same choice and every device follows it;
  * on Android the switch tells the app's native channel (Ringtone.appAlert, with a test notification), and a
    preference that arrives SYNCED from another device reaches the channel too -- the WebView itself never plays a
    notification sound there, so without that the choice would do nothing on the phone;
  * "Set as phone notification sound" hands the plugin the real alert as a NOTIFICATION, not a ringtone;
  * on the web/desktop an arriving notification plays the alert file instead of the synthesized chime.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_ringtone_settings_full_app import _open_tab


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


ANDROID = r"""window.Capacitor=window.Capacitor||{}; Capacitor.Plugins=Capacitor.Plugins||{};
window.__installs=[]; window.__appAlert=[];
Capacitor.Plugins.Ringtone={ available:async()=>({ok:true,canWrite:true,appAlert:false}),
  appAlert:async(o)=>{ __appAlert.push({on:!!o.on,test:!!o.test}); return {ok:true,appAlert:!!o.on}; },
  install:async(o)=>{ __installs.push({len:(o.data||'').length,name:o.name,kind:o.kind,setDefault:o.setDefault});
    return {ok:true,outcome:'set',where:'Notifications/posterchan-alert.ogg'}; } };"""
# Every <audio> the page plays, by file.
AUDIO_PROBE = r"""window.__played=[]; HTMLMediaElement.prototype.play=function(){ __played.push(String(this.src||'')); return Promise.resolve(); };"""

CHROME = pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")


@CHROME
def test_android_one_switch_turns_on_the_alert_channel_and_sets_the_phone_sound():
    got = {}

    async def check(b):
        await _open_tab(b)
        got["first"] = await b.js("[...document.querySelectorAll('[data-pane=\"ringtone\"] .rt-card b')].map(x=>x.textContent)[0]")
        got["tab"] = await b.js("document.querySelector('.us-tab[data-tab=\"ringtone\"]').textContent")
        got["sound"] = await b.js("fetch('/static/sounds/posterchan-alert.ogg').then(r=>r.arrayBuffer()).then(a=>a.byteLength)")
        await b.js("document.getElementById('al-app').click();true")
        await b.until("__appAlert.length>0 && /test notification/.test(document.getElementById('al-said').textContent)")
        got["pref"] = await b.js("__PC.notificationPreference('sound')")
        got["calls"] = await b.js("__appAlert")
        await b.js("document.getElementById('al-set').click();true")
        await b.until("__installs.length===1 && /notification sound now/.test(document.getElementById('al-said').textContent)")
        got["install"] = await b.js("__installs[0]")
        # Turned OFF on another device: the synced preference reaches the native channel without this page's switch.
        await b.js("window.__appAlert.length=0; __PC.setNotificationPreference('sound','chime'); true")
        await b.until("__appAlert.some(c=>c.on===false)")
        got["synced_off"] = await b.js("__appAlert")

    asyncio.run(desktop.with_browser("online", "", check, ANDROID))
    assert got["tab"].strip() == "Sounds" and got["first"] == "PosterChan Alert", got
    assert got["sound"] > 10_000, got
    assert got["pref"] == "posterchan", ("the switch did not set the account's arrival sound", got)
    assert {"on": True, "test": True} in got["calls"], got["calls"]
    i = got["install"]
    assert i["kind"] == "notification" and i["name"] == "posterchan-alert" and i["setDefault"] is True and i["len"] > 10_000, i
    assert {"on": False, "test": False} in got["synced_off"], got["synced_off"]


@CHROME
def test_on_the_web_a_notification_plays_the_alert_once_it_is_chosen():
    got = {}

    async def check(b):
        await _open_tab(b)
        got["links"] = await b.js("[...document.querySelectorAll('.al-card ~ .muted a[download], [data-pane=\"ringtone\"] a[download]')].map(a=>a.getAttribute('download'))")
        await b.js(AUDIO_PROBE)
        await b.js("__PC.notificationSound(); true")
        await asyncio.sleep(.6)
        got["before"] = await b.js("__played.slice()")
        await b.js("document.getElementById('al-app').click();true")
        await b.until("__PC.notificationPreference('sound')==='posterchan'")
        await asyncio.sleep(.6)
        await b.js("window.__played.length=0; __PC.notificationSound(); true")
        await asyncio.sleep(.3)
        got["after"] = await b.js("__played.slice()")
        got["select"] = await b.js("(()=>{const o=[...document.querySelectorAll('#us-notification-sound option')]; return o.length?o.map(x=>x.textContent):null})()")

    asyncio.run(desktop.with_browser("online", "", check))
    assert "posterchan-alert.ogg" in got["links"] and "posterchan-alert.mp3" in got["links"], got["links"]
    assert not any("posterchan-alert" in p for p in got["before"]), got["before"]
    assert any(p.endswith("/static/sounds/posterchan-alert.ogg") for p in got["after"]), ("no alert played", got["after"])
