"""PosterChanOS: the app's local-relay connection closes every query it opens, so it never runs out.

Measured on the desktop (.102) 2026-10-15: every read on the Store's local-relay socket was answered
`["CLOSED", id, "rate-limited: too many subscriptions"]`. `_lrQuery` sent REQ and, on EOSE, resolved -- but
never sent CLOSE, and a relay keeps a subscription open after EOSE until it is closed. After 64 reads (the
local relay's max_subs_per_conn) every later read returned nothing: Concord's cached control envelopes
(1,118 references for one community, every body still on the relay) read as missing, so the community's icon,
banner, channels and plane keys were gone ("plane key is not held", "channel is not readable", the icon that
"says saved but does not show"), minutes after every restart. The real bundled client against the SHIPPED
local relay process: one event, read back 150 times through Store.lrGet.
"""
import asyncio
import shutil
import time
from pathlib import Path

import pytest

from tests import os_relay_harness as H
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_local_relay_cache_full_app import INIT, PRE


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_reading_the_local_relay_never_runs_out_of_subscriptions():
    tmp = H.tmpdir()
    relay = H.LocalRelay(tmp)
    got = {}
    try:
        relay.start()
        e = H.ev(bytes([7] * 32), 1, "kept on the local relay", created_at=int(time.time()) - 60)

        async def seed():
            async with H.Client(relay.url) as c:
                ok, why = await c.publish(e)
                assert ok, why
        asyncio.run(seed())

        async def check(b):
            await desktop.login(b)
            await b.until("Store.backend()==='relay'")
            got['reads'] = await b.js("(async()=>{let n=0;for(let i=0;i<150;i++){const m=await Store.lrGet(['%s'],3000);if(m.size===1)n++;}return n})()" % e['id'])

        asyncio.run(desktop.with_browser('online', '', check, INIT.replace('LOCALRELAY', relay.url), pre_init=PRE))
    finally:
        relay.stop()
        shutil.rmtree(tmp, ignore_errors=True)
    assert got.get('reads') == 150, ('reads of a record the local relay holds came back empty', got)
