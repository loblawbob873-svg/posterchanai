"""Settings -> Phone -> "Screen report": the phone's own numbers for its bars and where the page sits.

"Android is still cutting off the top" (the Social search, the header, the Terminal's tabs) on a Galaxy
S25 and in a bug report from a 1080x2412 phone -- after two native fixes that each measured correctly on
the emulator. The third is made from the phone's facts: HomeScreen.screenReport (HomePlugin) answers
every inset the window reports and where the WebView sits on screen. Here: the row says in words whether
the page is under the status bar, Copy hands over the WHOLE report through copyValue (the clipboard path
that works in the APK), and an APK too old to have the call shows no row.
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


REPORT = {"model": "samsung SM-S931U", "sdk": 36, "density": 2.625, "screen": "1080x2340",
          "statusBarHeightRes": 63, "windowTopOnScreen": 0, "windowHeight": 2340,
          "pageTopOnScreen": 63, "pageTopInWindow": 63, "pageBottomOnScreen": 2277,
          "pageMargins": "0,63,0,63", "statusTop": 125, "statusTopIgnoringVisibility": 125,
          "stableTop": 125, "overlapTop": 62}


def native(with_call=True):
    call = ("screenReport:async()=>(%s)," % json.dumps(REPORT)) if with_call else ""
    return r'''
window.__copied=[];
window.Capacitor={isNativePlatform:()=>true,getPlatform:()=> 'android',Plugins:{HomeScreen:{
 consumeLaunchView:async()=>({view:''}), addListener:()=>({remove(){}}), formFactor:async()=>({form:'phone'}),
 setSystemBarsHidden:async o=>o, systemBars:async()=>({hidden:false}), %s
}}};
{const ex=document.execCommand.bind(document);document.execCommand=function(c){if(c==='copy'){const a=document.activeElement;window.__copied.push(a&&('value' in a)?a.value:String(getSelection()));return true;}return ex.apply(document,arguments);};}
try{Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async t=>{window.__copied.push(t);}}});}catch(_){}''' % call


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_report_says_the_page_is_under_the_status_bar_and_copies_every_number():
    got = {}

    async def check(b):
        await _phone_pane(b)
        await b.until("/px/.test((document.querySelector('#us-screen-sum')||{}).textContent||'')")
        got["sum"] = await b.js("document.querySelector('#us-screen-sum').textContent")
        got["fits"] = await b.js("(()=>{const r=document.querySelector('#us-screen-row').getBoundingClientRect();return r.width>0&&r.right<=innerWidth+1})()")
        await b.js("document.querySelector('#us-screen-copy').click()")
        await b.until("__copied.length>0 || /copied/.test(document.body.innerText)")
        got["copied"] = await b.js("__copied[0]||''")

    asyncio.run(desktop.with_browser("online", "", check, native()))
    assert "62px under the status bar" in got["sum"] and "samsung SM-S931U" in got["sum"], got
    assert got["fits"] is True, got
    rep = json.loads(got["copied"])
    assert {k: rep[k] for k in REPORT} == REPORT, "the copied report dropped or changed a number"
    assert "innerHeight" in rep and "scrollTop" in rep, "the page's own half of the report is missing"


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_an_apk_without_the_call_shows_no_report_row():
    got = {}

    async def check(b):
        await _phone_pane(b)
        got["row"] = await b.js("!!document.querySelector('#us-screen-row')")

    asyncio.run(desktop.with_browser("online", "", check, native(with_call=False)))
    assert got["row"] is False, got


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_the_report_measures_width_and_says_when_the_page_is_wider_than_the_screen():
    """A Galaxy Z Fold report ("image cuts off when unfolded") carried only the vertical half: every bar inset
    and where the page starts, and no width at all -- so a page cut off at the SIDE was invisible to the one
    tool built to diagnose it. The report now carries the page's width, zoom and pixel ratio, and says out
    loud when the page is wider than the screen it is drawn on."""
    got = {}

    async def check(b):
        await _phone_pane(b)
        await b.until("/px/.test((document.querySelector('#us-screen-sum')||{}).textContent||'')")
        await b.js("(()=>{const d=document.createElement('div');d.id='wide';d.style.cssText='width:'+(innerWidth+120)+'px;height:4px';document.body.appendChild(d)})()")
        await b.js("document.querySelector('#us-screen-copy').click()")
        await b.until("__copied.length>0")
        got["copied"] = await b.js("__copied[0]||''")
        got["sum"] = await b.js("document.querySelector('#us-screen-sum').textContent")
        got["iw"] = await b.js("innerWidth")

    asyncio.run(desktop.with_browser("online", "", check, native()))
    rep = json.loads(got["copied"])
    for k in ("innerWidth", "devicePixelRatio", "zoom", "overflowX", "vvWidth", "vvScale"):
        assert k in rep, "the report has no %s: %s" % (k, sorted(rep))
    assert rep["innerWidth"] == got["iw"] and rep["overflowX"] >= 100, rep
    assert "wider than the screen" in got["sum"], got["sum"]
