"""EVERY System Settings control opens -- and Bluetooth can actually pair a device.

"i can't add any bluetooth devices on OS! toaster says bluetoothpop is not defined!" (2026-10-17): System Settings
calls osshell.js openControl(kind), and its 'bluetooth' branch called a function that had never been written.
Nothing ran it, so nothing caught it. This runs the SHIPPED osshell.js in the real bundled client: every kind
openControl knows (read from its source, so a control added later is covered) must open without throwing; the
Bluetooth panel must list devices, scan, and send Pair to the machine's bridge.
"""
import asyncio
import re
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_native_power_confirm_browser import POWER

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()

BRIDGES = POWER + r'''
window.__bt=[];
window.pcBluetooth={status:async(scan)=>({available:true,powered:true,devices:scan?
   [{address:'AA:BB:CC:DD:EE:01',name:'Car stereo',audio:true,paired:false,connected:false},
    {address:'AA:BB:CC:DD:EE:02',name:'Headphones',audio:true,paired:true,connected:true}]
  :[{address:'AA:BB:CC:DD:EE:02',name:'Headphones',audio:true,paired:true,connected:true}]}),
 power:async(on)=>{__bt.push(['power',on]);return{ok:true};},
 device:async(mac,act)=>{__bt.push([act,mac]);return{ok:true};}};
window.pcNet={status:async()=>({wifiOn:true,connected:{ssid:'home',signal:70}}),wifi:async()=>[{ssid:'home',signal:70,secure:true,inUse:true}],
 setWifi:async()=>({ok:true}),connect:async()=>({ok:true}),disconnect:async()=>({ok:true})};
window.__errs=[]; window.addEventListener('error',e=>__errs.push(String(e.message)));
window.addEventListener('unhandledrejection',e=>__errs.push(String(e.reason&&e.reason.message||e.reason)));
'''


def _kinds():
    src = (ROOT / "static/js/client/osshell.js").read_text()
    body = src[src.index("async function openControl(kind, anchor){"):]
    body = body[:body.index("throw new Error('unknown system setting")]
    return re.findall(r"if\(kind === '([a-z]+)'\) return", body)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_every_control_opens_and_bluetooth_pairs():
    kinds = _kinds()
    assert "bluetooth" in kinds and len(kinds) >= 4, kinds
    got = {}

    async def check(b):
        await b.until("!!window.PCOSShell && !!document.querySelector('#os-popup-host')")
        for k in kinds:
            err = await b.js("(async()=>{try{await PCOSShell.openControl(%r, document.querySelector('#os-popup-host'));return '';}"
                             "catch(e){return String(e&&e.message||e);}})()" % k)
            got[k] = err
        await b.js("PCOSShell.openControl('bluetooth', document.querySelector('#os-popup-host'))")
        await b.until("/Headphones/.test((document.querySelector('.os-pop')||{}).textContent||'')")
        got["back_into_mixer"] = await b.js("!!document.querySelector('.os-pop [data-os=mixer]')")
        await b.js("document.querySelector('.os-pop [data-bt-scan]').click()")
        await b.until("!!document.querySelector('.os-pop [data-bt-act=pair]')")
        await b.js("document.querySelector('.os-pop [data-bt-act=pair]').click()")
        await b.until("__bt.some(x=>x[0]==='pair')")
        got["bt"] = await b.js("__bt")
        got["errs"] = await b.js("__errs")

    asyncio.run(desktop.with_browser("online", "?pcpopup=tray", check, BRIDGES))
    broken = {k: v for k, v in got.items() if k in kinds and v}
    assert not broken, ("a System Settings control throws", broken)
    assert ["pair", "AA:BB:CC:DD:EE:01"] in got["bt"], got
    assert not got["back_into_mixer"], "opened from Settings, the panel offers a 'back' into a mixer it never came from"
    assert not [e for e in got["errs"] if "is not defined" in e], got["errs"]
