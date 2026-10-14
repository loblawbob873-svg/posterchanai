"""On PosterChanOS a file opened from Files gets a window of its own; the Files window is left alone.

2026-10-13: "Files -> Opening files should not reuse Files window, leave files alone and open new window". Every app
is its own compositor window and PCOS is off inside one, so Preview drew its sheet OVER the Files listing (and Office
and Code switched the Files window's view). Drives the SHIPPED bundle in both halves of the hand-off:

  * a Files window (`?pcwin=blossom`) opening a picture on This Computer asks the DESKTOP (its live same-origin
    `window.opener`) for a `doc:file:<key>` window, parks the open there as data, and keeps its own listing;
  * the `doc:file:<key>` window takes that job from its opener and shows the picture, and closing the preview
    closes the window.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

# The desktop renderer as a Files window sees it: a live opener that can open real windows.
DESKTOP_OPENER = ("window.__parked=[];window.__opened=[];Object.defineProperty(window,'opener',{configurable:true,value:{closed:false,"
                  "__PC:{parkFileJob:(k,j)=>{__parked.push([k,j]);return true},takeFileJob:()=>null},"
                  "PCOS:{isOn:()=>true},PCOSWin:{enabled:()=>true,routable:()=>true,open:(v,l)=>{__opened.push([v,l]);return {}}}}});true")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_files_window_hands_the_file_to_a_new_window_and_keeps_its_listing():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.documentElement.classList.contains('pc-oswin') && !!window.__PC")
        await b.js(DESKTOP_OPENER)
        await b.js("window.pcHost=Object.assign(window.pcHost||{},{read:async()=>Uint8Array.from(atob('%s'),c=>c.charCodeAt(0))});true" % PNG)
        await b.js("__PC.switchView('blossom');true")
        await asyncio.sleep(.6)
        await b.js("__PC.hostOpen('/home/me/Pictures/cat.png','cat.png','image/png');true")
        await asyncio.sleep(1.5)
        got['opened'] = await b.js("__opened")
        got['parked'] = await b.js("__parked")
        got['here'] = await b.js("({view:__PC.VIEW, preview:!!document.querySelector('.pv-host'),"
                                 "close:typeof window.close})")

    asyncio.run(desktop.with_browser('online', '?pcwin=blossom', check, ''))
    assert got['here']['preview'] is False, 'the preview was drawn over the Files window: %r' % got
    assert got['here']['view'] == 'blossom', 'the Files window left its listing: %r' % got
    assert len(got['opened']) == 1, 'the desktop was not asked for a window of its own: %r' % got
    view, label = got['opened'][0]
    assert view.startswith('doc:file:') and label == 'cat.png', got
    key = view.split(':', 2)[2]
    assert [k for k, _ in got['parked']] == [key], 'the open was not parked under the window\'s key: %r' % got
    job = json.loads(got['parked'][0][1])
    assert job['fn'] == 'hostPreview' and job['args'][0] == '/home/me/Pictures/cat.png', job


KEY = 'abcdefgh1234'
FILE_WINDOW_INIT = r'''
window.__closed=0;window.close=()=>{window.__closed++;};
Object.defineProperty(window,'opener',{configurable:true,value:{closed:false,
  __PC:{takeFileJob:k=>k==='KEY'?{fn:'hostPreview',args:['/home/me/Pictures/cat.png','cat.png','image/png'],syncRoot:''}:null}}});
window.pcHost=Object.assign(window.pcHost||{},{read:async()=>Uint8Array.from(atob('PNG'),c=>c.charCodeAt(0))});
'''.replace('KEY', KEY).replace('PNG', PNG)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_file_window_shows_the_file_and_closing_it_closes_the_window():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("!!window.__PC")
        # The window's own route: if boot already ran it the preview is up; otherwise route now.
        await asyncio.sleep(.8)
        if not await b.js("!!document.querySelector('.pv-host')"):
            await b.js("__PC.switchView('doc:file:%s');true" % KEY)
        await b.until("!!document.querySelector('.pv-host img')")
        got['img'] = await b.js("(()=>{const i=document.querySelector('.pv-host img');return {src:i.src.slice(0,5),"
                                "name:(document.querySelector('.pv-host').textContent||'').includes('cat.png')}})()")
        await b.js("document.querySelector('.pv-host .pv-x').click();true")
        await asyncio.sleep(.4)
        got['closed'] = await b.js("window.__closed")
        got['errors'] = await b.js("(window.__errors||[]).slice()")

    asyncio.run(desktop.with_browser('online', '?pcwin=doc:file:' + KEY, check, FILE_WINDOW_INIT))
    assert got['img'] == {'src': 'blob:', 'name': True}, 'the file window did not show the file: %r' % got
    assert got['closed'] == 1, 'closing the preview left an empty window behind: %r' % got
    assert not got['errors'], got
