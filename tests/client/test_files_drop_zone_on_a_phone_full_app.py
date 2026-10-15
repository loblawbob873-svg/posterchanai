"""Files' upload box on a phone is two even buttons -- not a drag-and-drop sentence with one button wrapped below.

"the UI for 'drop files/folders here or choose files or folder', that looks sloppy on mobile" (2026-10-15). Measured
at 390px: "Drop files/folders here, or" + [choose files] on one line, [choose folder] alone on the next, two
different widths -- and a phone has no drag and drop. Now the sentence is hidden there and the two choices share one
row at equal widths and tap height; the desktop keeps its sentence.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


NO_OS = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
         "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")
MEASURE = r'''(()=>{const r=s=>{const e=document.querySelector(s);if(!e)return null;const b=e.getBoundingClientRect();
  return {x:Math.round(b.x),y:Math.round(b.y),w:Math.round(b.width),h:Math.round(b.height),shown:b.width>0&&getComputedStyle(e).display!=='none'};};
  return {files:r('#bl-pick'),folder:r('#bl-pickfolder'),text:r('#bl-drop .dz-text')};})()'''


def _measure(width, mobile):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 844, 'deviceScaleFactor': 2 if mobile else 1, 'mobile': mobile})
        await desktop.login(b)
        await b.js("__PC.switchView('blossom')")
        await b.until("!!document.querySelector('#bl-drop')")
        await asyncio.sleep(0.8)
        got.update(await b.js(MEASURE))
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))
    return got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_on_a_phone_the_two_choices_are_an_even_pair():
    m = _measure(390, True)
    f, d = m['files'], m['folder']
    assert not m['text']['shown'], ('a phone is told to drag and drop', m)
    assert f['y'] == d['y'], ('the two buttons are on different rows', m)
    assert abs(f['w'] - d['w']) <= 2 and f['w'] >= 140, ('the two buttons are not an even pair', m)
    assert f['h'] >= 44 and d['h'] >= 44, ('below the 44px tap height', m)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_desktop_keeps_its_drop_sentence():
    m = _measure(1280, False)
    assert m['text']['shown'], m
