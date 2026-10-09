"""THE TASKBAR'S MENUS MUST WORK WHEN NO RELAY CAN BE REACHED.

Reported from the laptop on a strange network: "none of the taskbar widget menus work when posterchan
cant connect to relay" — "cant even switch wifi". That is the worst possible failure for these menus:
the network panel is how somebody FIXES a connection, so it must never depend on one.

The shipped bundle, the real popup routes, with relay sockets that either never connect (a captive
portal / blackholed network) or are refused at once. Each menu must paint, have working buttons, and
for the tray, reach the Wi-Fi panel.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


DEAD = {
    # A socket that never opens: the handshake hangs for ever, like a blackholed network.
    'never': r'''class DeadSocket extends EventTarget{static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
      constructor(u){super();this.url=String(u);this.readyState=0;(window.__deadSockets=(window.__deadSockets||0)+1);}
      send(){throw new DOMException('not open','InvalidStateError');} close(){this.readyState=3;}}
      window.WebSocket=DeadSocket;''',
    # A socket refused at once: error then close, every time.
    'refused': r'''class RefusedSocket extends EventTarget{static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
      constructor(u){super();this.url=String(u);this.readyState=0;(window.__deadSockets=(window.__deadSockets||0)+1);
        setTimeout(()=>{this.readyState=3;this.onerror&&this.onerror(new Event('error'));this.onclose&&this.onclose(new Event('close'));},5);}
      send(){throw new DOMException('not open','InvalidStateError');} close(){this.readyState=3;}}
      window.WebSocket=RefusedSocket;''',
}

# The machine's own Wi-Fi bridge, the way preload.js injects it.
NET = r'''window.pcNet={available:async()=>true,
  status:async()=>({wifi:{enabled:true,ssid:'Cafe'},online:false,connectivity:'none'}),
  wifi:async()=>([{ssid:'Home',signal:80,secure:true},{ssid:'Cafe',signal:60,secure:false,active:true}]),
  connect:async(ssid,pw)=>{(window.__joined=window.__joined||[]).push(ssid);return{ok:true};},
  forget:async()=>({ok:true}),radio:async()=>({ok:true}),bridges:async()=>[]};'''

PAINTED = "(()=>{const h=document.querySelector('#os-popup-host');if(!h)return null;const r=h.getBoundingClientRect();" \
          "return {h:Math.round(r.height),text:h.innerText.trim().slice(0,200),buttons:h.querySelectorAll('button').length}})()"


async def _popup(b):
    for _ in range(40):                      # 2s: a menu is something you click, not something you wait for
        got = await b.js(PAINTED)
        if got and got['h'] > 30 and got['buttons'] > 0:
            return got
        await asyncio.sleep(.05)
    return await b.js(PAINTED)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('dead', ['never', 'refused'])
@pytest.mark.parametrize('kind', ['tray', 'net', 'start', 'noti'])
def test_every_taskbar_menu_paints_and_has_buttons_without_a_relay(kind, dead):
    got = {}

    async def check(b):
        got['popup'] = await _popup(b)
        got['sockets'] = await b.js('window.__deadSockets||0')
        got['errors'] = await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '?pcpopup=' + kind, check, DEAD[dead] + NET))
    p = got['popup']
    assert p and p['h'] > 30 and p['buttons'] > 0, (kind, dead, 'the menu did not paint', got)
    assert not got['errors'], got['errors']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('dead', ['never', 'refused'])
def test_wifi_can_be_switched_from_the_tray_without_a_relay(dead):
    got = {}

    async def check(b):
        await _popup(b)
        # Quick Settings → the Wi-Fi tile → the network list.
        await b.js("(()=>{const t=document.querySelector('#os-popup-host [data-qs=net],#os-popup-host [data-os=net]');if(t)t.click();return !!t})()")
        for _ in range(40):
            got['rows'] = await b.js("[...document.querySelectorAll('#os-popup-host button,#os-popup-host [role=button]')].map(x=>x.innerText.trim()).filter(t=>/Home|Cafe/.test(t))")
            if got['rows']:
                break
            await asyncio.sleep(.05)

    asyncio.run(desktop.with_browser('online', '?pcpopup=tray', check, DEAD[dead] + NET))
    assert got.get('rows'), ('no Wi-Fi networks offered without a relay', got)

