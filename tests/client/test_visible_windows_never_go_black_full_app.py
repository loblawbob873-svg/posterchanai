"""A DESKTOP WINDOW YOU CAN SEE KEEPS ITS CONTENT -- IT NEVER GOES BLACK BECAUSE ANOTHER ONE WAS FOCUSED.

The Windows app, 2026-10-30: "DM screen just went black after a while", "telegram was the other window that went
black as well". The desk hibernates parked windows to save memory (os.js capParked: keep the N most recent, empty the
rest), and a computer that reports 4 GB or less keeps only 2 -- right for a tablet's single WebView, where only the
front window shows, wrong on a desktop where every window behind the front one is still ON SCREEN: its slot was
emptied and it showed as a black rectangle until clicked. The shipped desk in a mouse-driven 1600x900 browser
reporting 4 GB: five windows tiled, focus moved between them like a person does, every visible window still holds
content. (tests/client/test_tablet_desk_memory_full_app.py keeps the touch tablet's hibernation.)
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


LOW_MEM = "Object.defineProperty(Navigator.prototype,'deviceMemory',{get:()=>4,configurable:true});"

VISIBLE_EMPTY = r"""[...document.querySelectorAll('.osw')].filter(w=>!w.classList.contains('minimised') && w.offsetParent!==null)
  .map(w=>{const t=(w.querySelector('.osw-title')||{}).textContent||'';const body=w.querySelector('.osw-body')||w;
    return {t:t.trim(), n:body.getElementsByTagName('*').length};})"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_windows_on_screen_keep_their_content_on_a_four_gigabyte_desktop():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1600, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        await b.js("PCOS.enter();true")
        await b.until("document.body.classList.contains('os-on')")
        views = ['messages', 'notifications', 'social', 'bookmarks', 'calendar']
        for v in views:
            await b.js("PCOS.routeView(%s);true" % json.dumps(v))
            await asyncio.sleep(.8)
        await b.js("PCOS.arrangeInPage('grid');true")
        await asyncio.sleep(1.5)
        for v in views + views[:3]:                     # move between them, as a person does
            await b.js("PCOS.routeView(%s);true" % json.dumps(v))
            await asyncio.sleep(.6)
        await asyncio.sleep(1)
        got['stats'] = await b.js("PCOS.parkStats()")
        got['windows'] = await b.js(VISIBLE_EMPTY)

    asyncio.run(desktop.with_browser('online', '', check, RELAY + LOW_MEM))
    st = got['stats']
    assert len(got['windows']) >= 4 and st['parked'] + st['hibernated'] > st['keep'], ('not enough windows to hit the cap', got)
    assert st['hibernated'] == 0, ('a window on screen was hibernated (drawn black)', got)
