"""PBS ON A PHONE AND ON A DESKTOP: it fits, it says what a phone cannot do, and it follows the client's UI rules.

Run: venv-unified/bin/python -m pytest tests/client/test_pbs_layout_full_app.py
Screenshots: set PC_PBS_SHOTS=<dir>.

At 390 px (a phone, whose browser has NO getDisplayMedia) and 1280 px, the real bundled client, a studio with a
text overlay, a camera and a live microphone:

  * no sideways page scroll, and every panel inside the page width
  * the phone says screen capture is desktop-only; its Screen button is marked unavailable and pressing it says
    why (camera and overlays still work: the camera is on the canvas)
  * the header carries the PosterChan logo; every icon button is a sprite <svg><use href="#i-…">, never a glyph
  * an input inside a styled field shows no focus box of its own -- the field lights up (:focus-within)
  * every choose-one control is a segmented track whose thumb sits under the picked choice
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import pbs_fixture as F
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STATE = r"""(()=>{const W=innerWidth,root=document.querySelector('#pbs');
  const over=[...root.querySelectorAll('.pbs-panel,.pbs-stagewrap,.pbs-head,.pbs-add,.pbs-go,.pbs-field')].filter(e=>{const r=e.getBoundingClientRect();return r.width&&(r.right>W+1||r.left<-1)}).map(e=>e.className);
  const ibs=[...root.querySelectorAll('.pbs-ib,.pbs-add,.pbs-go,.pbs-arm')];
  const glyph=ibs.filter(b=>!b.querySelector('svg.ic use[href^="#i-"]')||/[←-⯿\u{1f300}-\u{1faff}]/u.test(b.textContent));
  const segs=[...root.querySelectorAll('.pbs-seg')].map(s=>{const th=s.querySelector('.pbs-seg-thumb'),on=s.querySelector('.pbs-chip.on');
    const a=th.getBoundingClientRect(),c=on.getBoundingClientRect();return Math.abs((a.left+a.width/2)-(c.left+c.width/2))<4&&
      [...s.querySelectorAll('.pbs-chip')].every(x=>x.scrollWidth<=x.clientWidth+1)});
  return {page:document.documentElement.scrollWidth<=W+1&&document.querySelector('#feed').scrollWidth<=document.querySelector('#feed').clientWidth+1,
    over,glyph:glyph.map(b=>b.id||b.className),segs,
    logo:!!root.querySelector('.pbs-head .pbs-logo img[src]'),
    mobileNote:!!root.querySelector('.pbs-mobile-note'),screenOff:root.querySelector('#pbs-add-screen').getAttribute('aria-disabled')};})()"""

FOCUS = r"""(()=>{const i=document.querySelector('#pbs-title');i.focus();const f=i.closest('.pbs-field');
  const cs=getComputedStyle(i),fs=getComputedStyle(f);
  return {inputOutline:cs.outlineStyle,inputShadow:cs.boxShadow,fieldShadow:fs.boxShadow,fieldBorder:fs.borderColor};})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_pbs_fits_and_follows_the_ui_rules(width):
    got = {}

    async def check(b):
        await F.open_pbs(b, width)
        await F.click(b, '#pbs-add-text')
        await F.click(b, '#pbs-add-camera')
        await F.click(b, '#pbs-mic-on')
        await asyncio.sleep(.8)
        got['cam'] = await F.count(b, (0, 255, 0), None, 30)
        got['state'] = await b.js(STATE)
        got['focus'] = await b.js(FOCUS)
        await b.js("document.activeElement.blur();window.scrollTo(0,0);document.querySelector('#feed').scrollTop=0;true")
        await asyncio.sleep(.3)
        await F.shot(b, 'layout-%d-top.png' % width)
        await b.js("document.querySelector('#pbs-mixer').scrollIntoView({block:'center'});true")
        await asyncio.sleep(.3)
        await F.shot(b, 'layout-%d-mixer.png' % width)
        if width < 600:
            await F.click(b, '#pbs-add-screen')
            await asyncio.sleep(.3)
            got['why'] = await b.js("document.querySelector('#pbs-why').textContent")
            got['layers'] = await b.js("[...document.querySelectorAll('.pbs-layer-n')].map(n=>n.textContent)")
    asyncio.run(desktop.with_browser('online', '', check, F.init(__noScreen=width < 600)))
    s = got['state']
    assert s['page'] and not s['over'], ('PBS scrolls sideways or a panel overflows', s['over'])
    assert s['logo'], 'the PosterChan logo belongs in the header'
    assert not s['glyph'], ('icon buttons must use the sprite', s['glyph'])
    assert s['segs'] and all(s['segs']), ('a segmented thumb is off its pick or a label clips', s['segs'])
    f = got['focus']
    assert f['inputOutline'] == 'none' and f['inputShadow'] == 'none', ('the input draws its own focus box', f)
    assert f['fieldShadow'] != 'none', ('the field must light up on focus', f)
    assert got['cam']['n'] > 1000, 'the camera works on every width'
    if width < 600:
        assert s['mobileNote'] and s['screenOff'] == 'true', s
        assert 'desktop-only' in got['why'] and 'Screen' not in got['layers'], got
    else:
        assert not s['mobileNote'] and s['screenOff'] is None, s
