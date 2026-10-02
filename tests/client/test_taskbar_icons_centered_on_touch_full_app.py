"""Desktop-mode taskbar icons sit in the middle of their buttons on a tablet.

'taskbar icons are not centered on tablet' (the PosterChan desktop taskbar): the touch rule gave the
fixed-width, icon-only window buttons 13px of side padding, leaving a 14px track for a 20px icon --
measured on a 1340x800 touch tablet, the button painted 28px wide with its icon 6.7px off centre.
The real bundled client, in desktop mode, at tablet sizes with touch, and at a laptop size without.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MEASURE = r"""(()=>{const bar=document.querySelector('.os-bar');if(!bar)return null;const out=[];
  for(const el of bar.querySelectorAll('.os-task, .os-start, .os-net, .os-bell')){const r=el.getBoundingClientRect();if(!r.width)continue;
    const ic=el.querySelector('svg,img');if(!ic)continue;const q=ic.getBoundingClientRect();
    out.push({c:String(el.className).slice(0,24),w:r.width,iw:q.width,
      dx:Math.abs(q.left+q.width/2-(r.left+r.width/2)),dy:Math.abs(q.top+q.height/2-(r.top+r.height/2))});}
  return out;})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('w,h,dpr,touch', [(1340, 800, 2, True), (800, 1340, 2, True), (1440, 900, 1, False)])
def test_taskbar_icons_are_centered(w, h, dpr, touch):
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=w, height=h, deviceScaleFactor=dpr, mobile=touch))
        if touch:
            await b.call('Emulation.setTouchEmulationEnabled', dict(enabled=True, maxTouchPoints=5))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && !PCOS.isOn()) PCOS.enter(); }catch(e){}")
        await b.until("!!document.querySelector('.os-bar')")
        await b.js("__PC.switchView('notes')")
        await b.until("!!document.querySelector('.os-bar .os-task')")
        await asyncio.sleep(.4)
        if touch:
            assert await b.js("matchMedia('(pointer:coarse)').matches"), "touch emulation did not take"
        items = await b.js(MEASURE)
        assert items and any(i['c'].startswith('os-task') for i in items), items
        for i in items:
            assert i['w'] >= i['iw'], f"{i['c']}: button narrower than its icon ({i})"
            assert i['dx'] <= 1 and i['dy'] <= 1, f"{i['c']}: icon off centre by {i['dx']:.1f},{i['dy']:.1f}px ({w}x{h})"
    asyncio.run(desktop.with_browser('online', '', check))
