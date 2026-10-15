"""Music → "Add music" never drops you on the social timeline.

"add music button bringing him back to social timeline" (Vyram, 2026-10-15). Add music opens Files on the Music
folder, and with no instance and no media server of your own Files is not available -- switchView silently replaced
it with the timeline. Reproduced in the shipped bundle at phone width: no instance -> view 'global' ("Nostrverse").
Now it stays on Music and says what it needs. With an instance it still opens File Manager -- and a media server
that answers its listing with an object no longer throws "list.filter is not a function" out of Files.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


NO_OS = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse("
         "localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));")


def _run(standalone):
    got = {}

    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 2, 'mobile': True})
        await desktop.login(b)
        if standalone:
            await b.js("window.__PC_API_BASE__='';true")
        await b.js("window.__PC.openMusic ? __PC.openMusic() : __PC.switchView('music')")
        await b.until("!!document.querySelector('#ma-add')")
        await b.js("document.querySelectorAll('.toast').forEach(t=>t.remove());document.querySelector('#ma-add').click()")
        await asyncio.sleep(1.5)
        got['view'] = await b.js("__PC.VIEW")
        got['toasts'] = await b.js("[...document.querySelectorAll('.toast')].map(t=>t.textContent).join(' | ')")
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))
    return got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_without_files_add_music_stays_on_music_and_says_what_it_needs():
    got = _run(standalone=True)
    assert got['view'] != 'global', ('Add music landed on the social timeline', got)
    assert 'Settings → Media' in got['toasts'], ('it did not say what adding music needs', got)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_with_an_instance_add_music_opens_files_without_an_error():
    got = _run(standalone=False)
    assert got['view'] == 'blossom', got
    assert 'action failed' not in got['toasts'], ('Files threw on a listing that is not a list', got)
