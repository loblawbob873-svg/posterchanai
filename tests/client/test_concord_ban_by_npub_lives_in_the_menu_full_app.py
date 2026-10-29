"""Concord: "Ban by npub" is in the room's ☰ menu, not a stray button in the header (2026-10-29: "it's in posterchan but
it's right of the Room Description. I wanted it in the hamburger menu").

Every room action sits in the header as a button the ☰ menu presses, hidden from the header by one CSS rule
(concord.css, `header:has(#cc-call) :is(...)`). #cc-ban-npub was missing from that list. The real client, a real
minted CORD community this account owns (the harness of test_concord_switch_is_steady_full_app.py), on a phone and on
a desktop window: the header shows no Ban button, and ☰ lists "Ban by npub…".
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL
from tests.client.test_concord_switch_is_steady_full_app import BUILD, IMAGES, LATENCY, NO_OS, settle


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [390, 1280])
def test_ban_by_npub_is_a_menu_row_not_a_header_button(width):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=width, height=844, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js(NO_OS)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js("__PC.switchView('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL); await b.js(LATENCY); await b.js(IMAGES)
        counts = await b.js(BUILD)
        await b.js('PCConcord.render()')
        await b.until("!!document.querySelector('[data-cc-server=\"0\"]')")
        await b.js("document.querySelector('[data-cc-server=\"0\"]').click()")
        await b.until("!!document.querySelector('[data-cc-channel=\"general\"]')")
        await b.js("document.querySelector('[data-cc-channel=\"general\"]').click()")
        await b.until(f"document.querySelectorAll('.cc-message').length>={counts['Alpha/general']}")
        await settle(b, 2)
        got['header'] = await b.js("""(()=>{const h=document.querySelector('.cc-conversation>header'), x=h&&h.querySelector('#cc-ban-npub');
          return {exists: !!x, shown: !!x && x.getClientRects().length>0 && getComputedStyle(x).display!=='none'};})()""")
        await b.js("document.querySelector('#cc-head-more').click()")
        await b.until("[...document.querySelectorAll('.menu-pop button')].length>0")
        got['menu'] = await b.js("[...document.querySelectorAll('.menu-pop button')].map(e=>e.textContent.trim())")

    asyncio.run(desktop.with_browser('online', '', check))
    assert got['header']['exists'], ("the owner is not offered Ban by npub at all", got)
    assert not got['header']['shown'], ("Ban by npub is a button in the room header", got)
    assert any(t.startswith('Ban by npub') for t in got['menu']), ("Ban by npub is not in the ☰ menu", got['menu'])
