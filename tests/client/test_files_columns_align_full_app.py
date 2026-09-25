"""Desktop File Manager: the left column's first row and the middle column's toolbar line up.

Reported: "File Manager on Desktop/OS -> Left column and center column not aligned, the middle column
is slightly lower". Measured in a real desktop Files window (the bundled client, osMode on): the
first location in the sidebar ("Files") and the toolbar's first row (Locations/Back/Up, the crumb, the
search box) must share a centre line, within 2px. Before the fix the middle row sat ~6px lower.
"""
import asyncio
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MEASURE = r'''(()=>{const w=[...document.querySelectorAll('.osw')].pop(); if(!w) return null;
  const c=e=>{if(!e)return null;const r=e.getBoundingClientRect();return r.top+r.height/2};
  const side=w.querySelector('.fx-side .fx-tree-head');
  const row=['#fx-back','.fx-crumb.on','#fx-find'].map(s=>c(w.querySelector(s))).filter(v=>v!=null);
  return {side:c(side), main:row, sideTop:side&&side.getBoundingClientRect().top,
          barTop:(w.querySelector('.fx-bar')||{getBoundingClientRect:()=>({top:null})}).getBoundingClientRect().top};})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_sidebar_and_toolbar_share_a_centre_line_in_a_desktop_window():
    async def check(b):
        await desktop.login(b)
        await b.until("!!document.querySelector('#os-desk [data-view=\"blossom\"]')")
        await b.js("document.querySelector('#os-desk [data-view=\"blossom\"]').click()")
        await b.until("!!document.querySelector('.osw .fx-side .fx-tree-head') && !!document.querySelector('.osw #fx-find')")
        await asyncio.sleep(.3)
        m = await b.js(MEASURE)
        assert m and m['side'] is not None and m['main'], m
        for y in m['main']:
            assert abs(y - m['side']) <= 2, f"toolbar row centre {y:.1f} vs sidebar row {m['side']:.1f}: {m}"

    asyncio.run(desktop.with_browser('online', '', check))
