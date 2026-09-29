"""The Git page's Mine / Starred / All repos bar is always there for a signed-in person.

Reported with two screenshots: with 568 repos loaded the bar was gone, with 1,657 it was back — "What
I want is the bar always showed". It was drawn only when one of YOUR repos was among those loaded,
so a slow or partial load took all three scopes off the page. The real bundled client; the relay is
the fixture socket serving other people's repos and none of yours.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope="module", autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()


SETUP = r"""
(() => {
  const now = Math.floor(Date.now()/1000);
  window.__events = Array.from({length: 6}, (_, i) => ({ id: String(i).repeat(64).slice(0,64).replace(/^./, 'a'), kind: 30617,
    pubkey: String(i+1).repeat(64).slice(0,64), created_at: now - i*60, sig: 'e'.repeat(128), content: '',
    tags: [['d', 'repo'+i], ['name', 'Repo number '+i]] }));
  __PC.switchView('repos');
})()
"""


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
@pytest.mark.parametrize("width", [390, 1280])
def test_the_scope_bar_is_there_even_when_none_of_your_repos_loaded(width):
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=width, height=900, deviceScaleFactor=1, mobile=width < 600))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.until("document.querySelectorAll('.repo-card').length>=6")
        scopes = await b.js("[...document.querySelectorAll('.repo-scope .repo-sc')].map(x=>x.dataset.scope)")
        assert scopes == ["mine", "starred", "all"], f"the scope bar is missing: {scopes}"
        assert await b.js("document.querySelector('.repo-sc.on').dataset.scope") == "all", "opened on an empty scope"
        await b.js("document.querySelector('.repo-sc[data-scope=\"mine\"]').click()")
        await b.until("!!document.querySelector('#repo-results .empty')")
        assert "None of your repos" in await b.js("document.querySelector('#repo-results .empty').textContent")
        await b.js("document.querySelector('.repo-sc[data-scope=\"starred\"]').click()")
        await b.until("/No starred repos yet/.test(document.querySelector('#repo-results').textContent)")
        assert await b.js("document.querySelectorAll('.repo-scope .repo-sc').length") == 3, "choosing an empty scope removed the bar"
        await b.js("document.querySelector('.repo-sc[data-scope=\"all\"]').click()")
        await b.until("document.querySelectorAll('.repo-card').length>=6")

    asyncio.run(desktop.with_browser("online", "", check))


@pytest.mark.skipif(not Path("/opt/google/chrome/chrome").exists(), reason="Chrome required")
def test_coming_back_never_shows_fewer_repos_than_it_had():
    """Reported: "after I write some issues and press back, it can happen" — Git came back on an
    empty Starred with no search box. Back re-enters the list at once; an answer that came back short
    (a REQ into a socket still connecting is dropped) was painted as the relay's whole answer. The
    repos this device has already seen now stay, and a short answer can only add to them."""
    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", dict(width=390, height=900, deviceScaleFactor=1, mobile=True))
        await desktop.login(b)
        await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")
        await b.js(SETUP)
        await b.until("document.querySelectorAll('.repo-card').length>=6")
        await b.js("document.querySelector('.repo-sc[data-scope=\"starred\"]').click()")
        # Away (an issue, a repo), then Back — with the relay answering nothing this time.
        await b.js("__PC.switchView('notifications')")
        await b.js("window.__events=[]; __PC.switchView('repos')")
        await asyncio.sleep(1.5)
        await b.js("document.querySelector('.repo-sc[data-scope=\"all\"]').click()")
        await asyncio.sleep(.3)
        got = await b.js("({cards:document.querySelectorAll('.repo-card').length, search:!!document.querySelector('#repo-q')})")
        assert got == {"cards": 6, "search": True}, ("coming back lost the repos it had", got)

    asyncio.run(desktop.with_browser("online", "", check))
