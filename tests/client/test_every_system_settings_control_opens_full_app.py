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


CROWDED = POWER + r'''
window.__bt=[];
const __anon=Array.from({length:10},(_,i)=>({address:'4B:63:A7:9F:65:'+String(i).padStart(2,'0'),name:'4B-63-A7-9F-65-'+String(i).padStart(2,'0'),named:false,audio:false,paired:false,connected:false}));
window.pcBluetooth={status:async(scan)=>({available:true,powered:true,devices:[...__anon,
   {address:'DC:2C:26:00:11:22',name:'Arteck HB030B Universal Slim Portable Keyboard',named:true,input:true,paired:false,connected:false},
   {address:'2C:08:EF:08:03:F7',name:"Bob's Tab S10 Lite",named:true,paired:true,connected:false}]}),
 power:async()=>({ok:true}), device:async(mac,act)=>{__bt.push([act,mac]);return{ok:true};}};
'''


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_bluetooth_fits_the_flyout_and_lists_names_not_mac_addresses():
    """"bluetooth does not fit on the widget screen right ... i don't see my arteck keyboard by name ... mostly mac
    addresses" (2026-10-17). At the tray flyout's size: the named keyboard is a row with a keyboard icon, the ten
    anonymous advertisers fold into one closed line, nothing spills sideways and the list stays in the window."""
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 360, "height": 560, "deviceScaleFactor": 1, "mobile": False})
        await b.until("!!window.PCOSShell && !!document.querySelector('#os-popup-host')")
        await b.js("PCOSShell.openControl('bluetooth', document.querySelector('#os-popup-host'))")
        await b.until("/Arteck/.test((document.querySelector('.os-pop')||{}).textContent||'')")
        got.update(await b.js(r"""(()=>{const p=document.querySelector('.os-pop'),l=p.querySelector('.os-bt-list');
          const rows=[...l.querySelectorAll(':scope>.os-bt-row')].map(r=>r.querySelector('.os-bt-name b').textContent);
          const anon=l.querySelector('details.os-bt-anon');
          const kb=[...l.querySelectorAll('.os-bt-row')].find(r=>/Arteck/.test(r.textContent));
          const pr=p.getBoundingClientRect(), lr=l.getBoundingClientRect();
          const spill=[...p.querySelectorAll('*')].some(e=>{const r=e.getBoundingClientRect();return r.width&&(r.right>pr.right+1||r.left<pr.left-1);});
          return {rows, anonOpen: anon?anon.open:null, anonText: anon?anon.querySelector('summary').textContent:'',
            kbIcon: !!(kb&&kb.querySelector('use[href="#i-keyboard"]')), spill, listBottom: Math.round(lr.bottom), vh: innerHeight};})()"""))

    asyncio.run(desktop.with_browser("online", "?pcpopup=tray", check, CROWDED))
    assert any("Arteck" in r for r in got["rows"]) and len(got["rows"]) == 2, ("named devices are not what is listed", got)
    assert not any(":" in r or "-" in r[:3] for r in got["rows"]), ("a MAC address is still a listed row", got)
    assert got["anonOpen"] is False and got["anonText"].startswith("10 unnamed"), got
    assert got["kbIcon"], ("the keyboard has no keyboard icon", got)
    assert not got["spill"], ("something sticks out of the flyout", got)
    assert got["listBottom"] <= got["vh"], ("the list runs past the window", got)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_no_quick_settings_tile_spills_out_of_the_flyout():
    """"the bluetooth widget button text is too much, goes to the right" (2026-10-17): every tile stays inside the
    flyout at its real size, its text clipped with an ellipsis rather than overflowing."""
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": 360, "height": 560, "deviceScaleFactor": 1, "mobile": False})
        await b.until("!!document.querySelector('.os-pop .os-qs-tile[data-os=bluetooth]')")
        got.update(await b.js(r"""(()=>{const p=document.querySelector('.os-pop').getBoundingClientRect();
          return {tiles:[...document.querySelectorAll('.os-pop .os-qs-tile')].map(t=>{const r=t.getBoundingClientRect();
            return {k:t.dataset.os, out:r.right>p.right+1||r.left<p.left-1, spill:[...t.querySelectorAll('b,span')].some(e=>e.getBoundingClientRect().right>r.right+1)};})};})()"""))

    asyncio.run(desktop.with_browser("online", "?pcpopup=tray", check, BRIDGES))
    bad = [t for t in got["tiles"] if t["out"] or t["spill"]]
    assert got["tiles"] and not bad, ("a tile or its text sticks out of the flyout", got)


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [260, 360])
def test_a_connected_device_row_never_overlaps_its_buttons(width):
    """"bluetooth buttons on connected devices look ugly and overlaps" (2026-10-17): at the TV's scaled flyout width
    the name and "Connected" ran under Disconnect/Forget. The name stays whole and no button covers it."""
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 620, "deviceScaleFactor": 1, "mobile": False})
        await b.until("!!document.querySelector('.os-pop [data-os=bluetooth]')")
        await b.js("document.querySelector('.os-pop [data-os=bluetooth]').click()")
        await b.until("/Keyboard/.test((document.querySelector('.os-pop')||{}).textContent||'')")
        got.update(await b.js(r"""(()=>{const hit=(a,c)=>!(a.right<=c.left||c.right<=a.left||a.bottom<=c.top||c.bottom<=a.top);
          return {rows:[...document.querySelectorAll('.os-pop .os-bt-row')].map(r=>{const n=r.querySelector('.os-bt-name'),nb=n.querySelector('b');
            const nr=n.getBoundingClientRect();
            return {name:nb.textContent, clipped:nb.scrollWidth>nb.clientWidth+1,
                    overlap:[...r.querySelectorAll('.os-bt-actions .btn')].some(btn=>hit(nr,btn.getBoundingClientRect()))};})};})()"""))

    connected = POWER + r'''
window.pcBluetooth={status:async()=>({available:true,powered:true,devices:[
 {address:'D1:01:09:03:96:AE',name:'Bluetooth 5.1 Keyboard',named:true,input:true,paired:true,connected:true},
 {address:'2C:08:EF:08:03:F7',name:"Bob's Tab S10 Lite",named:true,paired:true,connected:false}]}),
 power:async()=>({ok:true}),device:async()=>({ok:true})};'''
    asyncio.run(desktop.with_browser("online", "?pcpopup=tray", check, connected))
    assert got["rows"] and not [r for r in got["rows"] if r["overlap"]], ("a button covers the device's name", got)
    assert not [r for r in got["rows"] if r["clipped"]], ("a device name is cut short", got)
