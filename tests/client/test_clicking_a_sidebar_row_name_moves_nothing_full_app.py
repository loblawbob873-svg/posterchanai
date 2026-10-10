"""Clicking a row's NAME in Settings -> Sidebar must not move the row.

Found while fixing "I still can't uncheck enabled startup apps": a click inside a <label> activates the label's
FIRST control, and a sidebar row is a <label> whose first control is its "Move up" button -- so clicking the
name of any row (that can move) would push it up the list.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_startup_switch_unchecks_with_a_real_click_full_app import _real_click


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_clicking_a_row_name_keeps_the_order():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));true")
        await b.js("__PC.switchView('settings')")
        await b.until("!!document.querySelector('.us-tab[data-tab=\"sidebar\"]')")
        await b.js("document.querySelector('.us-tab[data-tab=\"sidebar\"]').click()")
        await b.until("document.querySelectorAll('#nav-hide-list .nav-hide-row[data-navrow] [data-ordup]').length>2")
        order = "[...document.querySelectorAll('#nav-hide-list .nav-hide-row[data-navrow]')].map(r=>r.dataset.navrow)"
        got['before'] = await b.js(order)
        # the third movable row's NAME text
        await _real_click(b, "[...document.querySelectorAll('#nav-hide-list .nav-hide-row[data-navrow]')].filter(r=>r.querySelector('[data-ordup]'))[2].querySelector('span[style*=\"flex:1\"]')")
        await asyncio.sleep(.5)
        got['after'] = await b.js(order)

    asyncio.run(desktop.with_browser('online', '', check, ''))
    assert got['before'] and got['after'] == got['before'], ("clicking a row's name moved it", got)
