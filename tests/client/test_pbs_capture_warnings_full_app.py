"""PBS SAYS WHEN A SHARED WINDOW HAS FROZEN, AND WHEN IT IS SHARING ITSELF.

Reported live (2026-10-30, Firefox): "I see 'Add media from Files' on the live stream ... despite minimizing
everything". The Screen source was the Firefox window PBS runs in; it was captured while the Files picker was open, and
a minimised window sends no frames, so viewers kept seeing that last picture. Real clicks on the shipped studio:

  * the capture asks browsers to leave THIS tab out of their picker (`selfBrowserSurface: 'exclude'`);
  * a shared window that stops sending frames is named in the status line within seconds, and the warning clears
    itself when frames come back;
  * a shared surface titled like PosterChan/PBS is flagged: viewers see the studio.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import pbs_fixture as F
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


WHY = "(()=>{const w=document.querySelector('#pbs-why');return w&&!w.hidden?w.textContent:''})()"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_frozen_or_self_shared_window_is_said_out_loud():
    got = {}

    async def check(b):
        await F.open_pbs(b, 1280)
        await F.click(b, '#pbs-add-screen')
        await b.until("[...document.querySelectorAll('.pbs-layer-n')].some(n=>n.textContent==='Screen')")
        await asyncio.sleep(2)
        got['opts'] = await b.js("({self:(__gdmOpts||{}).selfBrowserSurface||''})")
        got['flowing'] = await b.js(WHY)
        await b.js("window.__freezeScreen=true;true")             # the shared window stops sending frames
        await asyncio.sleep(6)
        got['frozen'] = await b.js(WHY)
        await b.js("window.__freezeScreen=false;true")
        await asyncio.sleep(3)
        got['thawed'] = await b.js(WHY)
        # a surface titled like the studio itself
        await b.js("""(()=>{const g=navigator.mediaDevices.getDisplayMedia.bind(navigator.mediaDevices);
          navigator.mediaDevices.getDisplayMedia=async c=>{const s=await g(c);Object.defineProperty(s.getVideoTracks()[0],'label',{value:'PosterChan · Nostr — Mozilla Firefox'});return s;};return true})()""")
        await F.click(b, '#pbs-p-repick')
        await asyncio.sleep(1)
        got['self'] = await b.js(WHY)
    asyncio.run(desktop.with_browser('online', '', check, F.init()))
    assert got['opts']['self'] == 'exclude', got['opts']
    assert 'isn’t sending a picture' not in got['flowing'], got['flowing']
    assert 'isn’t sending a picture' in got['frozen'] and 'minimized' in got['frozen'], ('a frozen window must be named', got['frozen'])
    assert 'isn’t sending a picture' not in got['thawed'], ('the warning must clear when frames return', got['thawed'])
    assert 'sharing PosterChan itself' in got['self'], got['self']
