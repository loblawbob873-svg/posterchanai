"""SNAP LAYOUTS: hover a window's Maximise and pick where it goes -- halves, thirds, two-thirds + a third, quarters.

Asked 2026-10-28 ("snap layouts and spaces"). The flyout used to offer seven single zones (two halves, four quarters,
full screen); a wide screen wants thirds. Real mouse input on the shipped desktop: the hover opens the cards, a click
on a cell puts the window exactly in that part of the desktop, and the cards fit the screen.
"""
import asyncio
import base64
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _mouse(b, x, y, kind='mouseMoved'):
    await b.call('Input.dispatchMouseEvent', {'type': kind, 'x': x, 'y': y, 'button': 'left' if kind != 'mouseMoved' else 'none',
                                              'clickCount': 1 if kind != 'mouseMoved' else 0})


async def _center(b, sel):
    return await b.js("(()=>{const r=document.querySelector(%r).getBoundingClientRect();return [r.left+r.width/2,r.top+r.height/2]})()" % sel)


async def _click(b, sel):
    x, y = await _center(b, sel)
    await _mouse(b, x, y)
    await asyncio.sleep(.1)
    await _mouse(b, x, y, 'mousePressed')
    await _mouse(b, x, y, 'mouseReleased')


WIN = "#os-root .osw.focused"
GEOM = """(()=>{const w=document.querySelector('#os-root .osw.focused').getBoundingClientRect(),d=document.querySelector('#os-desk').getBoundingClientRect();
  return {l:(w.left-d.left)/d.width,r:(w.right-d.left)/d.width,t:(w.top-d.top)/d.height,b:(w.bottom-d.top)/d.height}})()"""


@CHROME
def test_hovering_maximise_offers_thirds_and_a_cell_places_the_window():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1600, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on')")
        await b.js("PCOS.routeView('settings');true")
        await b.until("!!document.querySelector('%s')" % WIN)
        await asyncio.sleep(.4)
        x, y = await _center(b, WIN + " .osw-b[data-w=max]")
        await _mouse(b, x, y)
        await b.until("!!document.querySelector('.os-layouts')")
        got['cards'] = await b.js("[...document.querySelectorAll('.os-snapcard')].map(c=>c.getAttribute('aria-label'))")
        got['under'] = await b.js("(()=>{const m=document.querySelector('.os-layouts').getBoundingClientRect(),"
                                  "b=document.querySelector('%s .osw-b[data-w=max]').getBoundingClientRect();"
                                  "return {gap:m.top-b.bottom, dx:Math.abs((m.left+m.width/2)-(b.left+b.width/2))}})()" % WIN)
        got['fits'] = await b.js("(()=>{const r=document.querySelector('.os-layouts').getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight})()")
        if os.environ.get('PC_SHOTS'):
            Path(os.environ['PC_SHOTS']).mkdir(parents=True, exist_ok=True)
            Path(os.environ['PC_SHOTS'], 'snap-layouts.png').write_bytes(
                base64.b64decode((await b.call('Page.captureScreenshot', {'format': 'png'}))['data']))
        await _click(b, ".os-snapcell[data-z=l23]")
        await asyncio.sleep(.4)
        got['l23'] = await b.js(GEOM)
        got['flyout_gone'] = await b.js("!document.querySelector('.os-layouts')")
        x, y = await _center(b, WIN + " .osw-b[data-w=max]")
        await _mouse(b, x, y)
        await b.until("!!document.querySelector('.os-layouts')")
        await _click(b, ".os-snapcell[data-z=r3]")
        await asyncio.sleep(.4)
        got['r3'] = await b.js(GEOM)

    asyncio.run(desktop.with_browser('online', '', check))
    assert 'Thirds' in got['cards'] and 'Two thirds and a third' in got['cards'] and 'Quarters' in got['cards'], got['cards']
    assert got['fits'], "the layouts flyout runs off the screen"
    assert 0 <= got['under']['gap'] < 20 and got['under']['dx'] < 60, ("the flyout did not open under Maximise", got['under'])
    g = got['l23']
    assert abs(g['l']) < .02 and abs(g['r'] - 2 / 3) < .02 and g['t'] < .02 and g['b'] > .97, ("not the left two thirds", g)
    assert got['flyout_gone']
    g = got['r3']
    assert abs(g['l'] - 2 / 3) < .02 and g['r'] > .98, ("not the right third", g)
