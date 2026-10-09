"""A DM WRITTEN WITH NO NETWORK IS SENT WHEN THE NETWORK COMES BACK.

Offline, a DM was wrapped (the signer is local), shown in the thread — and its publish failed with
nothing to retry it: a message that LOOKED sent and never arrived. Gift wraps are signed once at compose
time and addressed by their own id, so they belong in the Outbox (re-sending is a no-op). Two phases in
ONE browser profile: with every relay socket dead, send a DM through the shipped sendDm — both wraps must
be queued and the person told; then reload with the relay back — the queue must reach the relay.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


DEAD = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
if(localStorage.getItem('__dead')==='1'){
  Object.defineProperty(navigator,'onLine',{configurable:true,get:()=>false});
  window.WebSocket=class extends EventTarget{static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
    constructor(u){super();this.url=String(u);this.readyState=0;} send(){} close(){this.readyState=3;}};
} else window.__publishOK=true;   // back online: the relay accepts, as a real one would
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_dm_sent_offline_is_queued_then_delivered():
    got = {}

    async def check(b):
        await desktop.login(b)
        await b.js("localStorage.setItem('__dead','1')")
        await b.call('Page.reload')
        await b.until('!!window.__PC && !!__PC.me() && !!window.Outbox')
        await b.js("__PC.sendDm(NostrTools.getPublicKey(new Uint8Array(32).fill(9)),'see you at the ferry')")
        for _ in range(150):
            got['queued'] = await b.js("Outbox.list().map(x=>x.ev.kind)")
            if len(got['queued']) >= 2:
                break
            await asyncio.sleep(.1)
        got['ids'] = await b.js("Outbox.list().map(x=>x.ev.id)")
        got['toast'] = await b.js("[...document.querySelectorAll('.toast')].map(t=>t.textContent).join(' | ')")
        await b.js("localStorage.removeItem('__dead')")
        await b.call('Page.reload')
        await b.until('!!window.__PC && !!__PC.me() && !!window.Outbox')
        for _ in range(200):
            got['published'] = await b.js("(window.__published||[]).map(e=>e.id)")
            if all(i in got['published'] for i in got['ids']):
                break
            await asyncio.sleep(.1)
        got['left'] = await b.js("Outbox.count()")


    asyncio.run(desktop.with_browser('online', '', check, DEAD))
    assert got['queued'] == [1059, 1059], ("the DM written offline was not queued", got)
    assert "sent when you" in got['toast'], ("the person was not told it is waiting", got['toast'])
    assert all(i in got['published'] for i in got['ids']), ("the queued DM never reached the relay", got)
    assert got['left'] == 0, ("the delivered DM is still waiting in the queue", got)
