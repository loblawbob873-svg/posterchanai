"""Budget on a phone: after adding a plan there is a visible way back to the main (Bills) screen.

Reported: "After adding a plan, no way to go back to main budget screen". Adding a plan lands on the Plans
tab; the Bills tab was bare text with a transparent border beside a framed Plans tab, so it did not read
as something to tap. Also here: "↺ Reset month" and every row's "☰" were text glyphs that render as
empty boxes where the font lacks them (the same tofu the Files toolbar had).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


CLASSIC = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
           "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_after_adding_a_plan_the_bills_tab_is_a_visible_way_back():
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        await b.js("__PC.switchView('budget')")
        await b.until("!!document.querySelector('.bg-tab[data-tab=\"plans\"]')")
        await b.js("document.querySelector('.bg-tab[data-tab=\"plans\"]').click()")
        await b.until("!!document.querySelector('#bg-cn')")
        await b.js("document.querySelector('#bg-cn').value='Trip';document.querySelector('#bg-csave').click()")
        await b.until("document.querySelectorAll('.bg-plan').length===1")
        got['bills'] = await b.js(r'''(()=>{const t=document.querySelector('.bg-tab[data-tab="bills"]');const cs=getComputedStyle(t);
          const r=t.getBoundingClientRect();
          return {visible:r.width>0&&r.height>0&&r.top>=0&&r.bottom<=innerHeight, border:cs.borderTopColor,
                  bg:cs.backgroundColor, on:t.classList.contains('on')}})()''')
        got['tofu'] = await b.js("/[\\u2630\\u21ba]/.test(document.querySelector('#feed').innerText)")
        await b.js("document.querySelector('.bg-tab[data-tab=\"bills\"]').click()")
        await b.until("!!document.querySelector('.bg-tab[data-tab=\"bills\"].on')")
        got['back'] = await b.js("!!document.querySelector('#bg-n') && !document.querySelector('#bg-cn')")

    asyncio.run(desktop.with_browser('online', '', check, CLASSIC))
    t = got['bills']
    assert t['visible'] and not t['on'], got
    assert 'rgba(0, 0, 0, 0)' not in t['border'] and 'rgba(0, 0, 0, 0)' not in t['bg'], \
        f"the Bills tab does not look like a button: {t}"
    assert got['back'], "tapping Bills did not return to the main budget screen"
    assert not got['tofu'], "text glyphs (☰ ↺) that draw as empty boxes are back"
