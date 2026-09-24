"""A taskbar search opens its results IN FRONT on PosterChanOS -- as a real window.

Reported: "search from taskbar is opening Search window behind other windows". The results were a
`doc:` frame on the DESKTOP SURFACE, and every app on PosterChanOS is a compositor toplevel above that
surface, so the Search frame could never come in front of Social or Messages. Like a post, a search is
rebuilt from its own name -- the query -- so it opens as its own toplevel carrying it.

Both halves through the shipped bundle: the desktop asks for a `doc:search` window with the query (and
draws no in-page frame), and a window opened as `?pcwin=doc:search&pcq=...` runs that search.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_desktop_asks_for_a_real_search_window():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.body.classList.contains('os-on') && !!document.querySelector('#os-q-bar')")
        await b.js("document.getElementById('osfr')?.remove();document.documentElement.classList.remove('osfr-on')")
        # The compositor side: on PosterChanOS PCOSWin is enabled; here the request is recorded.
        await b.js("window.__asked=[];PCOSWin.enabled=()=>true;PCOSWin.open=(v,l,o)=>{__asked.push([v,o&&o.arg]);return {}};true")
        frames = await b.js("document.querySelectorAll('.osw').length")
        await b.js("(()=>{const q=document.querySelector('#os-q-bar');q.focus();q.value='cats and dogs';"
                   "q.dispatchEvent(new Event('input',{bubbles:true}));"
                   "q.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));})();true")
        await asyncio.sleep(.5)
        got['asked'] = await b.js("__asked")
        got['new_frames'] = (await b.js("document.querySelectorAll('.osw').length")) - frames

    asyncio.run(desktop.with_browser('online', '', check, ''))
    assert got['asked'] == [['doc:search', 'cats and dogs']], got
    assert got['new_frames'] == 0, "an in-page Search frame was drawn behind the apps: %r" % got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_search_window_runs_the_query_it_was_opened_with():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.until("document.documentElement.classList.contains('pc-oswin') && !!window.PCOS")
        await b.until("!!document.querySelector('.os-srch')")
        got['consumed'] = await b.js("!new URLSearchParams(location.search).has('pcq')")
        got['search'] = await b.js("__PC.isView('search')")

    asyncio.run(desktop.with_browser('online', '?pcwin=doc:search&pcq=cats%20and%20dogs', check, ''))
    assert got['consumed'] and got['search'], got
