"""Texts -> Add an attachment, measured in the real renderer rather than read as markup.

Reported as "Improve the UI for Texts -> Add Attachment, looks ugly". Measured before the change:
three bare `.btn`s butted together with no gap ("Camera photo" | "Device" | "📁 Files" -- one emoji,
two words), a Camera item on a desktop browser that ignores `capture` and so opened the same dialog
as Device, and a composer row whose paperclip/emoji buttons were 32px beside a 41px Send and a 45px
input (the input's own 6px margin pushed it off the row's centre line).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_sms_image_paste_full_app import open_texts


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


MEASURE = r"""(()=>{
  const box=e=>{const r=e.getBoundingClientRect();return {l:r.left,t:r.top,r:r.right,b:r.bottom,w:r.width,h:r.height}};
  const rows=[...document.querySelectorAll('.sms-attach-sources > *')].map(e=>({id:e.id,...box(e),
    icon:!!e.querySelector('svg use'),sub:(e.querySelector('small')||{}).textContent||''}));
  const row=[...document.querySelectorAll('.sms-compose > *')].filter(e=>e.getBoundingClientRect().width>0)
    .map(e=>({id:e.id,...box(e)}));
  const rawFile=[...document.querySelectorAll('input[type=file]')].filter(e=>e.getBoundingClientRect().width>0).length;
  return {rows,row,rawFile,vw:innerWidth};
})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('phone', [True, False])
def test_attachment_sources_are_a_styled_list_and_the_composer_row_lines_up(phone):
    async def check(b):
        w, h = (390, 844) if phone else (1280, 900)
        await b.call('Emulation.setDeviceMetricsOverride', {'width': w, 'height': h, 'deviceScaleFactor': 1, 'mobile': phone})
        if phone:
            await b.call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
        await open_texts(b)
        await b.js("window.__picked=[];__PC.blossomPicker=(...a)=>__picked.push(a)")
        await b.js("document.querySelector('#sms-attach').click()")
        await b.until("document.querySelectorAll('.sms-attach-sources > *').length>0")
        m = await b.js(MEASURE)

        ids = [r['id'] for r in m['rows']]
        want = (['sms-src-camera'] if phone else []) + ['sms-src-device', 'sms-src-blossom']
        assert ids == want, ids  # a desktop browser has no camera to offer
        assert m['rawFile'] == 0, 'a raw file input is visible'
        for r in m['rows']:
            assert r['icon'] and r['sub'].strip(), r            # icon + where it comes from
            assert r['l'] >= 0 and r['r'] <= m['vw'], r         # inside the viewport
            if phone:
                assert r['h'] >= 44, r                          # a touch target
        widths = {round(r['w']) for r in m['rows']}
        assert len(widths) == 1, widths                         # one column, not a ragged toolbar
        for a, c in zip(m['rows'], m['rows'][1:]):
            assert c['t'] - a['b'] >= 4, (a, c)                 # rows do not touch

        heights = {round(r['h']) for r in m['row']}
        assert len(heights) == 1, m['row']                     # attach/emoji/input/send line up
        tops = {round(r['t']) for r in m['row']}
        assert len(tops) == 1, m['row']
        if phone:
            assert min(heights) >= 40, m['row']

        # The restyle kept the bindings: a source still reaches its picker.
        await b.js("document.querySelector('#sms-src-blossom').click()")
        await b.until("__picked.length===1")
        assert await b.js("!document.querySelector('.sms-attach-sources')")

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))
