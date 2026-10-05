"""Settings → Notifications says when push is ON but the phone is NOT connected.

"I never got push notification for my last reminder on my phone" / "of course I had push notifications on".
On the Android app the status came from `getEndpoint().endpoint` -- "this phone registered once" -- and then
promised calls, messages and reminders while the push service had not connected to the server for two
weeks (measured: last_seen 2026-09-21). The service reports `connected` and its last error; the screen now
reads them. Real client, the phone's push plugin stubbed at the Capacitor boundary.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def plugin(state):
    return ("window.Capacitor=window.Capacitor||{};window.Capacitor.Plugins=Object.assign(window.Capacitor.Plugins||{},{PosterChanPush:{"
            "register:async()=>({ok:true}),setPrefs:async()=>{},stayConnected:async()=>({on:false}),"
            "batteryStatus:async()=>({ignoring:true}),"
            f"getEndpoint:async()=>({json.dumps(state)})}}}});true")


async def status(b, state):
    await desktop.login(b)
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
    await b.js(plugin(state))
    await b.js("__PC.switchView('settings');true")
    await b.until("!!document.getElementById('set-push-status')")
    await asyncio.sleep(9.5)            # the screen gives a fresh start up to 8 s to connect
    return await b.js("({text:document.getElementById('set-push-status').textContent, warn:document.getElementById('set-push-status').classList.contains('push-warn'), btn:document.getElementById('set-push-toggle').textContent})")


CASES = {
    "disconnected": ({"endpoint": "pcdirect:dev1", "deviceId": "dev1", "connected": False, "error": "connection closed",
                      "notificationsEnabled": True}, "connection closed"),
    "wrong-address": ({"endpoint": "pcdirect:dev1", "deviceId": "dev1", "connected": True, "needsRegistration": True,
                       "notificationsEnabled": True}, "different server address"),
    "notifications-blocked": ({"endpoint": "pcdirect:dev1", "deviceId": "dev1", "connected": True,
                               "notificationsEnabled": False}, "blocked"),
}


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("case", list(CASES))
def test_a_phone_that_is_not_connected_is_told_so(case):
    state, why = CASES[case]
    res = {}

    async def check(b):
        res.update(await status(b, state))
    asyncio.run(desktop.with_browser("online", "", check))
    assert "Turn off" in res["btn"], res                       # it IS switched on
    assert res["warn"] and "NOT connected" in res["text"] and why in res["text"], res


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_a_connected_phone_is_not_warned():
    res = {}

    async def check(b):
        res.update(await status(b, {"endpoint": "pcdirect:dev1", "deviceId": "dev1", "connected": True,
                                    "notificationsEnabled": True}))
    asyncio.run(desktop.with_browser("online", "", check))
    assert not res["warn"] and "NOT connected" not in res["text"] and res["text"].startswith("On"), res
