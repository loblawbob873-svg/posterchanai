"""ONE SWITCH HOLDS THE STREAM AT 720p: "Cap at 720p" in PBS's Output panel.

"we need an easy way to cap bandwidth at 720p in PBS" ... "as an option" (2026-10-30). The real bundled client, real
WebRTC session (tests/client/pbs_fixture.py), real clicks:

  default      the cap is ON: the canvas is 1280x720, 1080p and High are greyed, the header chip reads 720p30 · ≤1.5 Mbps
  off          1080p and High become choosable; picking them makes the canvas 1920x1080
  live         going live uncapped sends at 3000 kbps; turning the cap on WHILE LIVE re-caps the sender at 1500 kbps
               and brings the picture back to 1280x720 -- the choices underneath are kept for when it goes off again
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import pbs_fixture as F
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


STATE = r"""(()=>{const c=document.querySelector('#pbs-canvas'),q=s=>document.querySelector(s);
  return {cap:q('#pbs-cap').checked, w:c.width, h:c.height, chip:q('#pbs-capchip').textContent,
    d1080:q('.pbs-seg[data-seg="res"] .pbs-chip[data-v="1080"]').disabled, dHigh:q('.pbs-seg[data-seg="kbps"] .pbs-chip[data-v="3000"]').disabled,
    onRes:(q('.pbs-seg[data-seg="res"] .pbs-chip.on')||{}).dataset?.v, onKbps:(q('.pbs-seg[data-seg="kbps"] .pbs-chip.on')||{}).dataset?.v};})()"""


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_720p_cap_is_one_switch_and_holds_while_live():
    got = {}

    async def check(b):
        await F.open_pbs(b, 1280)
        got['default'] = await b.js(STATE)
        await F.click(b, '#pbs-cap')
        await asyncio.sleep(.3)
        await F.click(b, '.pbs-seg[data-seg="res"] .pbs-chip[data-v="1080"]')
        await F.click(b, '.pbs-seg[data-seg="kbps"] .pbs-chip[data-v="3000"]')
        await asyncio.sleep(.3)
        got['off'] = await b.js(STATE)
        await F.click(b, '#pbs-add-screen')
        await asyncio.sleep(.5)
        await F.click(b, '#pbs-go')
        await b.until("document.querySelector('#pbs-status').dataset.state==='live'")
        await asyncio.sleep(1)
        got['live_uncapped'] = await b.js('__setParams.slice(-1)[0]')
        await F.click(b, '#pbs-cap')
        await asyncio.sleep(.6)
        got['live_capped'] = await b.js('__setParams.slice(-1)[0]')
        got['capped'] = await b.js(STATE)
        got['errors'] = await b.js('__errors')
    asyncio.run(desktop.with_browser('online', '', check, F.init()))
    assert got['errors'] == [], got['errors']
    d = got['default']
    assert d['cap'] and (d['w'], d['h']) == (1280, 720) and d['d1080'] and d['dHigh'], ('the cap must be on by default', d)
    assert d['chip'] == '720p30 · ≤1.5 Mbps', d
    o = got['off']
    assert not o['cap'] and not o['d1080'] and not o['dHigh'] and (o['w'], o['h']) == (1920, 1080), ('the cap off must free 1080p/High', o)
    assert got['live_uncapped'][0]['maxBitrate'] == 3000000, got['live_uncapped']
    assert got['live_capped'][0]['maxBitrate'] == 1500000, ('turning the cap on while live must re-cap the sender', got['live_capped'])
    c = got['capped']
    assert (c['w'], c['h']) == (1280, 720) and c['onRes'] == '720' and c['onKbps'] == '1500' and c['chip'].startswith('720p'), c
