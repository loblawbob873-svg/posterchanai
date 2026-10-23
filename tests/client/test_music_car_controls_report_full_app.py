"""Settings -> Phone -> "Music & car controls": what the phone measured about the media buttons.

Reported: "car media controls no longer work on music" -- a plain Bluetooth car, PosterChan music playing in
the background, a press that does NOTHING AT ALL. From the driver's seat that is four different failures (the
press never reached the app; it reached the STANDBY session; it reached the real session and the page never
answered; no music session existed at all), and MusicControls.status() could already tell some of them apart
-- but no screen showed it. This row does: the session state in words, and the last presses with the session
that delivered each one and what came of it. Its two buttons are pressed here by id against the shipped
client, with a stand-in for the native plugin that answers the way MusicPlugin.status() does.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_hide_system_bars_full_app import _phone_pane


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIRST = {"running": True, "sessionActive": True, "standbySession": False, "pageListening": True,
         "playing": True, "webSilenceMs": 4000, "unanswered": 1, "revived": 0, "note": "",
         "logTag": "PosterChanMusic",
         "presses": [{"at": 1792790000000, "action": "pause",
                      "via": "PosterChanMusic session (car / headset / lock screen)",
                      "outcome": "answered — the page performed it"},
                     {"at": 1792789990000, "action": "play", "via": "standby session (PosterChanStandby)",
                      "outcome": "unanswered — asked the app to come back"}]}
SECOND = {"running": False, "standbySession": True, "pageListening": True, "unanswered": 2, "revived": 1,
          "note": "a different PosterChan task was closed", "logTag": "PosterChanMusic",
          "presses": [{"at": 1792790100000, "action": "next", "via": "media button with no player session",
                       "outcome": "handed to the page (no player session)"}]}


def native(with_status=True):
    status = ("status:async()=>{window.__musicReads=(window.__musicReads||0)+1;"
              "return window.__musicReads>1?%s:%s},") % (json.dumps(SECOND), json.dumps(FIRST)) if with_status else ""
    return r'''
window.__copied=[];
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=> 'android',Plugins:{
 HomeScreen:{consumeLaunchView:async()=>({view:''}), addListener:()=>({remove(){}}), formFactor:async()=>({form:'phone'}),
   setSystemBarsHidden:async o=>o, systemBars:async()=>({hidden:false})},
 MusicControls:{addListener:()=>({remove(){}}), setOptions:async()=>({}), ack:async()=>({}), update:async()=>({}),
   stop:async()=>({}), consumeLaunchAction:async()=>({action:''}), %s}
}};
{const ex=document.execCommand.bind(document);document.execCommand=function(c){if(c==='copy'){const a=document.activeElement;window.__copied.push(a&&('value' in a)?a.value:String(getSelection()));return true;}return ex.apply(document,arguments);};}
try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{window.__copied.push(t);}}});}catch(_){}''' % status


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_row_names_the_session_and_the_last_presses_and_its_buttons_work():
    got = {}

    async def check(b):
        await _phone_pane(b)
        await b.until("/media session/.test((document.querySelector('#us-music-sum')||{}).textContent||'')")
        got["sum1"] = await b.js("document.querySelector('#us-music-sum').textContent")
        got["presses1"] = await b.js("document.querySelector('#us-music-presses').textContent")
        got["fits"] = await b.js("(()=>{const r=document.querySelector('#us-music-row').getBoundingClientRect();"
                                 "return r.width>0&&r.right<=innerWidth+1})()")
        # Refresh, pressed by id: the phone is asked again and the new answer replaces the old one.
        await b.js("document.querySelector('#us-music-refresh').click()")
        await b.until("/not running/.test(document.querySelector('#us-music-sum').textContent)")
        got["sum2"] = await b.js("document.querySelector('#us-music-sum').textContent")
        got["presses2"] = await b.js("document.querySelector('#us-music-presses').textContent")
        # Copy, pressed by id: the WHOLE report, presses included, through copyValue.
        await b.js("document.querySelector('#us-music-copy').click()")
        await b.until("__copied.length>0")
        got["copied"] = await b.js("__copied[__copied.length-1]||''")
        got["reads"] = await b.js("window.__musicReads||0")

    asyncio.run(desktop.with_browser("online", "", check, native()))
    assert "media session: active" in got["sum1"] and "standby session: off" in got["sum1"], got
    assert "page listening: yes" in got["sum1"] and "unanswered presses 1" in got["sum1"], got
    assert "pause" in got["presses1"] and "PosterChanMusic session" in got["presses1"], got
    assert "answered — the page performed it" in got["presses1"], got
    assert "standby session (PosterChanStandby)" in got["presses1"], got
    assert got["fits"] is True, got
    assert "not running" in got["sum2"] and "standby session: on" in got["sum2"], got
    assert "media button with no player session" in got["presses2"] and "PosterChanStandby" not in got["presses2"], got
    rep = json.loads(got["copied"])
    assert rep["presses"] and rep["presses"][0]["action"] in ("next", "pause"), rep
    assert rep["logTag"] == "PosterChanMusic", rep
    assert got["reads"] >= 3, "Refresh/Copy did not ask the phone again: %r" % got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_apk_without_the_call_shows_no_music_row():
    got = {}

    async def check(b):
        await _phone_pane(b)
        got["row"] = await b.js("!!document.querySelector('#us-music-row')")

    asyncio.run(desktop.with_browser("online", "", check, native(with_status=False)))
    assert got["row"] is False, got
