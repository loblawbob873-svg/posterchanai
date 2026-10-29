"""Arrange tiles the windows ON THIS WORKSPACE, and fills the screen with them.

"Windows app not tiling correctly, so much wasted monitor space" (2026-10-29, screenshot: four windows each a third of
the screen wide and tall, the bottom half empty, workspaces 1 and 2 on the taskbar). Arrange counted every window that
was not minimised -- the ones parked on workspace 2 too -- so a 2x2 grid became a 3x3 one with five cells held by
windows that are not on screen. The shipped bundle as the desktop app runs it (the windowed desktop, in-page windows),
four apps on workspace 1 and three on workspace 2, Arrange -> grid on workspace 1; what is measured is the screen.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MEASURE = r"""(()=>{const bar=document.querySelector('#os-bar'), barTop=bar?bar.getBoundingClientRect().top:innerHeight;
 const ws=[...document.querySelectorAll('.osw')].filter(w=>getComputedStyle(w).display!=='none'&&getComputedStyle(w).visibility!=='hidden'&&w.getBoundingClientRect().width>0).map(w=>w.getBoundingClientRect());
 let cov=0,all=0; for(let y=5;y<barTop;y+=10)for(let x=5;x<innerWidth;x+=10){all++; if(ws.some(r=>x>=r.left&&x<=r.right&&y>=r.top&&y<=r.bottom))cov++;}
 return {visible:ws.length, coverage:Math.round(1000*cov/Math.max(1,all))/10};})()"""


@CHROME
@pytest.mark.parametrize('w,h', [(1920, 1080), (2560, 1707)])
def test_arrange_fills_the_screen_with_this_workspaces_windows(w, h):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=w, height=h, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && !PCOS.isOn()) PCOS.enter(); }catch(e){}")
        await b.until("!!document.querySelector('#os-bar')")
        for v in ('notes', 'calendar', 'contacts', 'global'):
            await b.js(f"PCOS.routeView('{v}')")
            await asyncio.sleep(.5)
        await b.until("document.querySelectorAll('.osw').length >= 4")
        await b.js("PCOS.addWorkspace()")            # opens workspace 2 and switches to it
        await asyncio.sleep(.4)
        for v in ('concord', 'messages', 'notifications'):
            await b.js(f"PCOS.routeView('{v}')")
            await asyncio.sleep(.5)
        await b.until("document.querySelectorAll('.osw').length >= 7")
        await b.js("PCOS.switchWorkspace(0)")
        await asyncio.sleep(.5)
        got['arranged'] = await b.js("JSON.stringify(PCOS.arrangeInPage('grid'))")
        await asyncio.sleep(.8)
        got['m'] = await b.js(MEASURE)

    asyncio.run(desktop.with_browser('online', '', check))
    m = got['m']
    assert m['visible'] == 4, ("expected the 4 windows of workspace 1 on screen", got)
    assert m['coverage'] >= 90, ("Arrange left the screen empty around this workspace's windows", got)
