"""Switching Git to "All repos" and back is quick, however many repos the relays hold.

Reported 2026-10-29 (Vyram): "When switching to all repos there is a long delay before it switches to it & it hangs
when trying to switch back." The relays answer well over a thousand repo announcements (the code's own note: 1,657),
and the switch drew a card for EVERY one -- each with its owner's avatar and a profile request -- then bound each card;
switching back tore all of them down. The SHIPPED git.js with 1,600 repos: All repos draws a first screenful at
once, the rest follow as the list is scrolled, search still finds a repo far down the list, and Mine comes back at once.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SEED = r"""
(() => {
  localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
  const me = __PC.me().pubkey, now = Math.floor(Date.now()/1000), hex = n => n.toString(16).padStart(64, '0');
  const evs = [];
  for (let i = 0; i < 1600; i++) {
    const own = i < 3, pk = own ? me : hex(100000 + (i % 400)), name = own ? 'mine-' + i : 'repo-' + i;
    evs.push({ id: hex(i + 1), kind: 30617, pubkey: pk, created_at: now - i * 60, sig: 'e'.repeat(128), content: '',
      tags: [['d', name], ['name', name], ['description', 'project number ' + i], ['clone', 'https://example.com/' + name + '.git']] });
  }
  window.__events = (window.__events || []).concat(evs);
  evs.forEach(e => { try{ __PC.Store ? __PC.Store.saveEvent(e) : 0; }catch(_){} });
  __PC.switchView('repos'); return 1;
})()
"""

CLICK = """(async s => { const b = document.querySelector('.repo-sc[data-scope="' + s + '"]'); const t0 = performance.now();
  b.click(); const sync = performance.now() - t0;
  await new Promise(r => requestAnimationFrame(() => setTimeout(r, 0)));
  return { sync: Math.round(sync), frame: Math.round(performance.now() - t0), cards: document.querySelectorAll('#repo-results .repo-card').length }; })"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_all_repos_and_back_is_quick(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width < 600})
        await desktop.login(b)
        await b.js(SEED)
        await b.until("!!document.querySelector('.repo-sc[data-scope=\"all\"]')")
        await b.js("document.querySelector('.repo-sc[data-scope=\"mine\"]').click(); 1")
        await b.until("document.querySelectorAll('#repo-results .repo-card').length === 3")
        # the full list has arrived (1,600) -- the placeholder counts what All repos would show
        await b.js("document.querySelector('.repo-sc[data-scope=\"all\"]').click(); 1")
        await b.until("/1600 repos/.test(document.querySelector('#repo-q').placeholder)")
        await b.js("document.querySelector('.repo-sc[data-scope=\"mine\"]').click(); 1")

        to_all = await b.js(f"({CLICK})('all')")
        back = await b.js(f"({CLICK})('mine')")
        print("switch", width, to_all, back)
        assert to_all["cards"] <= 120, ("All repos draws every card at once", to_all)
        assert to_all["cards"] >= 12, ("All repos shows too little to fill a screen", to_all)
        assert to_all["frame"] < 400, ("switching to All repos is slow", to_all)
        assert back["cards"] == 3 and back["frame"] < 400, ("switching back to Mine is slow", back)

        # the rest follow as the list is scrolled
        await b.js(f"({CLICK})('all')")
        first = await b.js("document.querySelectorAll('#repo-results .repo-card').length")
        await b.js("document.querySelector('#repo-results .repo-more').scrollIntoView(); 1")
        await b.until(f"document.querySelectorAll('#repo-results .repo-card').length > {first}")
        # search covers the whole list, not only what is drawn
        await b.js("(()=>{const q=document.querySelector('#repo-q');q.value='repo-1590';q.dispatchEvent(new Event('input'));})()")
        await b.until("[...document.querySelectorAll('#repo-results .repo-card-name')].some(n => n.textContent === 'repo-1590')")
        assert not await b.js("__errors.slice()")

    asyncio.run(desktop.with_browser("online", "?pcShell=1", check))
