"""THE GO LIVE SHEET LEADS TO PBS, KEEPS OBS UNDER "EXTERNAL ENCODER", AND ITS CAMERA PATH IS PBS'S PUBLISHER.

Run: venv-unified/bin/python -m pytest tests/client/test_pbs_golive_sheet_full_app.py

"i want nostr streaming simplified with pbs" -- one way to stream rather than several. The real bundled client:

  * the menu's Go Live sheet opens with "Open PBS Studio" first; pressing it lands in PBS
  * the OBS server / stream key / stream URL are still there for people with their own encoder, but folded under
    "External encoder" -- and picking "OBS / another encoder" unfolds them
  * the sheet's quick camera path goes live through the SAME WHIP publisher PBS uses (streams.js _whipConnect):
    the same codec preference and sender caps, and -- new with PBS -- Stop sends the WHIP DELETE for the session
    the server named, which the old code never did
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import pbs_fixture as F
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def _sheet(b):
    await b.js("(window.__PC&&__PC.goLive)?__PC.goLive():document.getElementById('nav-golive').click();true")
    await b.until("!!document.querySelector('#gl-pbs')")
    await asyncio.sleep(.3)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_sheet_offers_pbs_first_and_folds_the_obs_key():
    got = {}

    async def check(b):
        await F.open_pbs(b, 1280)
        await b.js("__PC.switchView('streams');true")
        await asyncio.sleep(.5)
        await _sheet(b)
        got['first'] = await b.js("(()=>{const m=document.querySelector('.modal');const kids=[...m.children].filter(e=>e.offsetParent);return kids[1]&&kids[1].id})()")
        got['ext_closed'] = await b.js("!document.querySelector('#gl-ext').open && !!document.querySelector('#gl-ext #gl-srv') && !!document.querySelector('#gl-ext #gl-key')")
        await b.js("(()=>{const r=document.querySelector('input[name=gl-src][value=obs]');r.checked=true;r.dispatchEvent(new Event('change',{bubbles:true}));return true})()")
        await asyncio.sleep(.2)
        got['ext_open'] = await b.js("document.querySelector('#gl-ext').open")
        await F.shot(b, 'golive-sheet-1280.png')
        await F.click(b, '#gl-pbs')
        await b.until("!!document.querySelector('#pbs #pbs-canvas')")
        got['modal_gone'] = await b.js("!document.querySelector('#gl-pbs')")
    asyncio.run(desktop.with_browser('online', '', check, F.init()))
    assert got['first'] == 'gl-pbs', ('PBS must lead the Go Live sheet', got['first'])
    assert got['ext_closed'], 'the OBS server and key belong under a folded External encoder section'
    assert got['ext_open'], 'picking OBS must show what OBS needs'
    assert got['modal_gone']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_quick_camera_path_publishes_through_the_shared_whip_code_and_ends_the_session():
    got = {}

    async def check(b):
        await F.open_pbs(b, 1280)
        await b.js("__PC.switchView('streams');true")
        await asyncio.sleep(.5)
        await _sheet(b)
        await b.js("""(()=>{const r=document.querySelector('input[name=gl-src][value=cam]');r.checked=true;r.dispatchEvent(new Event('change',{bubbles:true}));
            const f=document.querySelector('input[name=gl-facing][value=user]');f.checked=true;f.dispatchEvent(new Event('change',{bubbles:true}));
            document.querySelector('#gl-title').value='Quick cam';return true})()""")
        await asyncio.sleep(.2)
        await b.js("document.querySelector('#gl-go').click();true")
        await b.until("!!document.getElementById('phone-live') && __srvTracks.length>=2")
        await asyncio.sleep(1)
        got['params'] = await b.js('__setParams')
        got['tracks'] = await b.js('__srvTracks')
        await b.js("document.querySelector('#pl-stop').click();true")
        await b.until("__whipDel.length>0")
        got['deleted'] = await b.js('__whipDel')
        await asyncio.sleep(.5)
        got['ended'] = await b.js("__published.some(e=>e.kind===30311&&e.tags.some(t=>t[0]==='status'&&t[1]==='ended'))")
    asyncio.run(desktop.with_browser('online', '', check, F.init()))
    assert sorted(got['tracks'])[:2] == ['audio', 'video'], got['tracks']
    assert got['params'] and got['params'][0][0]['maxBitrate'] == 1500000, got['params']
    assert got['deleted'][0].endswith('/api/streams/whip/tok123/' + F.SID), got['deleted']
    assert got['ended']
