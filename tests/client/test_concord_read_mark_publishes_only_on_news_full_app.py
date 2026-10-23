"""Sitting in a Concord room does not republish the synced read marks on every repaint.

Measured live 2026-10-22 by scripts/check_concord_federation_live.py: ONE idle browser in a quiet room published the
encrypted `pcai:concord-read` document 27 times a minute -- markRead() started its synced mark from Date.now(), so
every render moved it forward and every move re-read, re-encrypted, re-signed and republished the doc (and a
dissolve queued behind those writes took minutes to go out). The shared mark is the newest message SEEN: repaints
publish nothing new, and a new message moves it exactly once more. Real minted community, the shipped client.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_fixture_relay import INSTALL, MINT, OPEN_ROOM
from tests.client.test_concord_reply_and_attach_full_app import send


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


READ_DOCS = ("(window.__published||[]).filter(e=>e.kind===30078&&(e.tags||[]).some(t=>t[0]==='d'&&t[1]==='pcai:concord-read')).length")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_repaints_publish_no_read_mark_and_a_new_message_moves_it_once():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
        await desktop.login(b)
        await b.js("window.__publishOK=true; __PC.switchMessagesTab('concord')")
        await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')
        await b.js(INSTALL)
        minted = await b.js(MINT.replace('NAME', json.dumps('Quiet room')))
        await b.js(OPEN_ROOM + '(' + json.dumps(minted['room']) + ')')
        await b.until("!!document.querySelector('#cc-input')")
        await send(b, 'hello there')
        await asyncio.sleep(2)                      # the mark for the message just seen settles
        before = await b.js(READ_DOCS)
        for _ in range(30):                         # a quiet room being repainted: live ticks, focus, resizes
            await b.js("PCConcord.render(); 1")
            await asyncio.sleep(.1)
        await asyncio.sleep(2)
        quiet = await b.js(READ_DOCS) - before
        assert quiet == 0, ('repainting a quiet room republished the read marks', quiet)
        await send(b, 'something new')
        await b.until(READ_DOCS + " > %d" % before)
        await asyncio.sleep(2)
        assert await b.js(READ_DOCS) - before == 1, 'a new message did not move the synced read mark exactly once'
        assert not await b.js('__errors'), await b.js('__errors')

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))
