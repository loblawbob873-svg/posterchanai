"""The avatar top right of a window's title row is the new round PosterChan badge ("maybe we should update the top
right window posterchan logo with your new one", 2026-10-29).

`--pc-logo` (client.css) draws it: a phone's title rows and every PosterChanOS window. It was the old hooded portrait
in a rounded square; it is the app icon's face badge now, drawn round. Measured on the shipped client at phone width,
where every screen's title row carries it: what the browser actually paints there.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_title_row_avatar_is_the_round_app_badge():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=390, height=844, deviceScaleFactor=3, mobile=True))
        await desktop.login(b)
        await b.js("__PC.switchView('notifications')")
        await b.until("!!document.querySelector('.main:not(.has-navbar)>.topbar')")
        got['after'] = await b.js("""(()=>{const t=document.querySelector('.main:not(.has-navbar)>.topbar'),cs=getComputedStyle(t,'::after');
          return {bg:cs.backgroundImage, radius:cs.borderTopLeftRadius, w:cs.width};})()""")
        got['loads'] = await b.js("""new Promise(r=>{const m=/url\\("?([^")]+)/.exec(getComputedStyle(document.querySelector('.main:not(.has-navbar)>.topbar'),'::after').backgroundImage);
          if(!m) return r(false); const i=new Image(); i.onload=()=>r(i.naturalWidth); i.onerror=()=>r(false); i.src=m[1];})""")

    asyncio.run(desktop.with_browser('online', '', check))
    a = got['after']
    assert 'posterchan-badge.png' in a['bg'], ("the window avatar is not the new badge", got)
    assert a['radius'] == '50%', ("the badge is not drawn round", got)
    assert got['loads'] and got['loads'] >= 108, ("the badge image does not load (or is too small for a 3x screen)", got)
