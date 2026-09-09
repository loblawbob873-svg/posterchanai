"""Real relay reload/firehose acceptance feeds the bundled app through a WS fixture."""
import asyncio
import json
import time
from pathlib import Path
import pytest
from app.services.nostr.event import build_event
from tests.test_relay_live_word_reload import runtime
from tests.client import test_desktop_offline_full_app as desktop

@pytest.fixture(scope='module',autouse=True)
def bundle():
    yield from desktop.bundle.__wrapped__()

@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(),reason='Chrome required')
def test_admin_url_reload_blocks_new_feed_rows_without_deleting_history():
    async def scenario():
        env,fresh,store,_=runtime();accepted=[]
        async def add(event,**kwargs):accepted.append(event);return True
        store.add_event.side_effect=add
        def note(text,offset):return build_event(bytes.fromhex('15'*32),1,text,created_at=int(time.time())+offset)
        old=note('Earlier stored https://theboard.world',-3)
        blocked=note('New spam HTTPS://THEBOARD.WORLD/path',-2)
        ordinary=note('An ordinary useful message',-1)
        await env['_firehose_event'](old)
        fresh['blocked_words']={'https://theboard.world'};await env['reload_blocks']()
        await env['_firehose_event'](blocked);await env['_firehose_event'](ordinary)
        async def check(b):
            await b.js('window.__events='+json.dumps(accepted))
            await desktop.login(b);await b.js("__PC.switchView('global')")
            await b.until('!!document.querySelector(\'[data-id="'+ordinary['id']+'"]\')')
            assert await b.js('!!document.querySelector(\'[data-id="'+old['id']+'"]\')'),'existing history is not retroactively deleted by reload'
            assert not await b.js('!!document.querySelector(\'[data-id="'+blocked['id']+'"]\')'),'new matching spam reached actual feed'
            await b.js("__PC.switchView('home');__PC.switchView('global')")
            assert not await b.js('!!document.querySelector(\'[data-id="'+blocked['id']+'"]\')')
        await desktop.with_browser('online','?pcShell=1',check)
    asyncio.run(scenario())
