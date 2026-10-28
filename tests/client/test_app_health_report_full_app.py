"""Settings -> Phone -> "Restarts & memory": why the app last ended, and the Copy button that sends it.

The tablet desktop "starts like a fresh new session" with no toast (2026-10-25) -- the whole process ending, which only
Android records (ApplicationExitInfo). The row shows AppHealth.report() in words; Copy hands over the whole report
plus what only the page can see (DOM nodes, JS heap, desktop windows). Pressed by id against the shipped client, with
a stand-in for the native plugin that answers the way AppHealthPlugin.report() does.
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


REPORT = {"sdk": 34, "device": "samsung SM-X200", "exitsSupported": True, "rendererDeaths": 0,
          "memory": {"totalMb": 3700, "availMb": 380, "thresholdMb": 300, "lowMemory": True, "appPssMb": 690,
                     "memoryClassMb": 256, "largeMemoryClassMb": 512, "lowRamDevice": False},
          "exits": [{"at": 1792790000000, "reason": "low-memory", "unexpected": True, "importance": "foreground",
                     "onScreen": True, "process": "place.poster.app", "pssMb": 905, "rssMb": 1210, "status": 0,
                     "description": "lmkd"},
                    {"at": 1792780000000, "reason": "user-requested", "unexpected": False, "importance": "cached",
                     "onScreen": False, "process": "place.poster.app", "pssMb": 300, "rssMb": 400, "status": 0,
                     "description": ""}]}


def native(with_report=True):
    report = ("report:async()=>{window.__healthReads=(window.__healthReads||0)+1;return %s},"
              % json.dumps(REPORT)) if with_report else ""
    return r'''
window.__copied=[];
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=> 'android',Plugins:{
 HomeScreen:{consumeLaunchView:async()=>({view:''}), addListener:()=>({remove(){}}), formFactor:async()=>({form:'tablet'}),
   setSystemBarsHidden:async o=>o, systemBars:async()=>({hidden:false})},
 AppHealth:{%s}
}};
{const ex=document.execCommand.bind(document);document.execCommand=function(c){if(c==='copy'){const a=document.activeElement;window.__copied.push(a&&('value' in a)?a.value:String(getSelection()));return true;}return ex.apply(document,arguments);};}
try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{window.__copied.push(t);}}});}catch(_){}''' % report


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_row_says_what_ended_the_app_and_copy_sends_the_whole_report():
    got = {}

    async def check(b):
        await _phone_pane(b)
        await b.until("/unexpected end/.test((document.querySelector('#us-health-sum')||{}).textContent||'')")
        got["sum"] = await b.js("document.querySelector('#us-health-sum').textContent")
        got["fits"] = await b.js("(()=>{const r=document.querySelector('#us-health-row').getBoundingClientRect();"
                                 "return r.width>0&&r.right<=innerWidth+1})()")
        await b.js("document.querySelector('#us-health-copy').click()")
        await b.until("__copied.length>0")
        got["copied"] = await b.js("__copied[__copied.length-1]||''")
        got["reads"] = await b.js("window.__healthReads||0")

    asyncio.run(desktop.with_browser("online", "", check, native()))
    assert "low-memory while you were using it" in got["sum"] and "905 MB" in got["sum"], got
    assert "380 / 3700 MB free (LOW)" in got["sum"], got
    assert got["fits"] is True, got
    rep = json.loads(got["copied"])
    assert rep["exits"][0]["reason"] == "low-memory" and rep["device"] == "samsung SM-X200", rep
    assert rep["page"]["nodes"] > 0, ("the page's own numbers are missing from the report", rep)
    assert got["reads"] >= 2, "Copy did not ask the phone again: %r" % got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_apk_without_the_plugin_shows_no_restart_row():
    got = {}

    async def check(b):
        await _phone_pane(b)
        got["row"] = await b.js("!!document.querySelector('#us-health-row')")

    asyncio.run(desktop.with_browser("online", "", check, native(with_report=False)))
    assert got["row"] is False, got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_row_says_how_busy_the_main_thread_was():
    """A tablet: "social took about 20 s, same for messages" -- and the report could not say whether those seconds were
    the CPU or the network. Three 300 ms tasks block the page; the row and the copy must count them."""
    got = {}

    async def check(b):
        # Real page tasks (a DevTools evaluate is not one): three 300 ms timers.
        await b.js("for(let i=0;i<3;i++)setTimeout(()=>{const t=performance.now();while(performance.now()-t<300){}},200*i+50);1")
        await asyncio.sleep(2)
        await _phone_pane(b)
        await b.until("/main thread busy/.test((document.querySelector('#us-health-sum')||{}).textContent||'')")
        got["sum"] = await b.js("document.querySelector('#us-health-sum').textContent")
        await b.js("document.querySelector('#us-health-copy').click()")
        await b.until("__copied.length>0")
        got["copied"] = json.loads(await b.js("__copied[__copied.length-1]||'{}'"))

    asyncio.run(desktop.with_browser("online", "", check, native()))
    busy = got["copied"]["page"]["busy"]
    assert busy["longTasks2m"] >= 3 and busy["busyMs2m"] >= 850 and busy["longestMs"] >= 290, got
    assert "main thread busy" in got["sum"], got
    assert "status" in got["copied"]["page"]["relay"], got
    holds = got["copied"]["page"]["holds"]
    assert holds["events"] >= 0 and holds["localStorageKb"] >= 0 and isinstance(holds["biggestKb"], list), got
    assert "holds " in got["sum"] and "KB saved data" in got["sum"], got
    # keys are named, account keys never: a 64-hex pubkey in a key name is replaced
    assert not any(len(x) > 0 and __import__("re").search(r"[0-9a-f]{64}", x) for x in holds["biggestKb"]), holds
