"""The PosterChanOS start menu's bottom bar -- your account chip and the power/settings buttons -- is ON SCREEN.

"you also broke the start menu, no more buttons or user account shows at the bottom!" (2026-10-16). The existing
popup tests asked whether `.os-foot .os-acct` EXISTED, and a footer that is in the DOM but drawn outside the window,
or at zero size, passes that. This opens the popup the way the OS does (`?pcpopup=start`, at the size _startPopup
asks the compositor for) and measures the footer: inside the viewport, visible, with its buttons hit-testable.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MEASURE = r"""(()=>{const f=document.querySelector('#os-startmenu .os-foot, .os-startmenu .os-foot');
  if(!f) return {foot:false};
  const r=f.getBoundingClientRect(), cs=getComputedStyle(f);
  const btns=[...f.querySelectorAll('button')].map(b=>{const q=b.getBoundingClientRect();
     const x=q.left+q.width/2,y=q.top+q.height/2,hit=document.elementFromPoint(x,y);
     return {id:b.id||b.className,w:q.width,h:q.height,top:q.top,bottom:q.bottom,hit:!!hit&&(hit===b||b.contains(hit))}});
  return {foot:true,top:r.top,bottom:r.bottom,h:r.height,vh:innerHeight,vw:innerWidth,vis:cs.visibility,
          disp:cs.display,op:cs.opacity,btns}})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('size,scale', [((780, 920), None), ((975, 1150), '1.25'), ((975, 700), '1.5'), ((500, 420), None)])
def test_the_start_popup_bottom_bar_is_on_screen(size, scale):
    """975x1150 at UI scale 1.25 is the measured popup on the 4K desk where the bottom bar vanished: the menu drew
    1438px tall in a 1150px window, its footer at y=1344."""
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': size[0], 'height': size[1],
                                                            'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        if scale:
            await b.js("document.body.style.setProperty('--ui-scale', %r)" % scale)
        await b.until("!!document.querySelector('.os-foot .os-acct')")
        await asyncio.sleep(1.5)
        got['zoom'] = await b.js("getComputedStyle(document.body).zoom")
        got.update(await b.js(MEASURE))

    asyncio.run(desktop.with_browser('online', '?pcpopup=start', check))
    assert got.get('foot'), got
    if scale:
        assert abs(float(got['zoom']) - float(scale)) < 0.01, ('the UI scale never applied, so this measured nothing', got)
    assert got['disp'] != 'none' and got['vis'] == 'visible' and float(got['op']) > 0.5, got
    assert got['h'] >= 24 and got['top'] >= 0 and got['bottom'] <= got['vh'] + 0.5, ('the bottom bar is off screen', got)
    assert len(got['btns']) >= 2, ('the bottom bar lost its buttons', got)
    assert all(b['hit'] and b['w'] > 8 and b['h'] > 8 for b in got['btns']), ('a bottom-bar button cannot be clicked', got)
