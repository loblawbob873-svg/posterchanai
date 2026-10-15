"""A CONCORD ATTACHMENT MAY BE UP TO 95 MB -- the 20 MB cap refused a webxdc game every server would have taken.

"i can't even upload my 20mb cat tail doom to concord, 20mb too large? should be 95 mb". The composer refused any
file over 20 MiB before asking anybody, while this node's Blossom takes 100 MB (blossom_max_upload_mb) and the edge
proxy in front of it 6 GB. Driven in the shipped bundle: a real community minted by PosterCord, the composer's own
file input, Blossom stubbed at uploadBlob.

  30 MB .xdc   (over the old cap)  -> uploaded, and its URL is in the composer
  100 MB file  (over the new cap)  -> refused before any upload, and the toast names the 95 MB limit
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_concord_icon_upload_says_so_full_app import NO_OS, ROOM, wait


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# ROOM's uploadBlob holds every upload open until __release(); release them straight away here.
INSTANT = "__PC.uploadBlob=async blob=>{__uploads.push({size:blob.size,state:'released'});return 'https://fixture.invalid/blossom/'+'d'.repeat(64);};"

CHOOSE = r'''((mb,name,type)=>{const f=document.querySelector('#cc-file');
const file=new File([new Uint8Array(mb*1024*1024)],name,{type});const dt=new DataTransfer();dt.items.add(file);
f.files=dt.files;f.dispatchEvent(new Event('change'));return true;})(%d,%r,%r)'''

TOASTS = "[...document.querySelectorAll('.toast')].map(t=>t.textContent).join(' | ')"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_30_mb_game_uploads_and_a_100_mb_file_is_refused_naming_the_limit():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("__PC.switchView('concord')")
        await b.until("!!window.PCConcord && !!window.PosterCord && document.body.classList.contains('concord-view')")
        await b.js(ROOM)
        await b.js(INSTANT)
        await b.js("__PC.switchView('concord')")
        await b.until("!!document.querySelector('#cc-file') && !!document.querySelector('#cc-input')")
        await b.js(CHOOSE % (30, 'cat-tail-doom.xdc', 'application/x-webxdc'))
        got['uploaded'] = await wait(b, "__uploads.length===1", 30)
        got['composer'] = await b.js("(document.querySelector('#cc-input')||{}).value||''")
        got['toasts_30'] = await b.js(TOASTS)
        await b.js(CHOOSE % (100, 'huge.bin', 'application/octet-stream'))
        await wait(b, TOASTS + ".includes('too large')", 15)
        got['toasts_100'] = await b.js(TOASTS)
        got['uploads'] = await b.js("__uploads.map(u=>u.size)")
    asyncio.run(desktop.with_browser('online', '', check, NO_OS))

    assert got['uploaded'], ('a 30 MB game was not uploaded', got)
    assert 'fixture.invalid/blossom/' in got['composer'], ('the uploaded game is not in the message', got)
    assert 'too large' not in got['toasts_30'], got
    assert 'huge.bin is too large (95 MB max)' in got['toasts_100'], ('the 100 MB refusal does not name the limit', got)
    assert got['uploads'] == [30 * 1024 * 1024], ('a file over the limit was uploaded anyway', got['uploads'])
