"""Bookmarks opened after a missed list read show the relays' list, not the old copy on this device.

The gate's real-node bookmark test kept failing under load with one of two bookmarks listed after a reload and no
error. After a reload BOOKMARKS comes from the kind-10003 the Store held -- an OLDER copy when the second bookmark's
list had not been persisted yet -- and the boot's read of the relays' copy timed out to [] (complete:false). Nothing
asked again, so the old list stayed for the whole session. Here the page holds every kind-10003 read until the boot's
read has timed out, the Store holds the old one-bookmark list, the relay the new two-bookmark one; then Bookmarks is
opened. Real bundled client, real Chrome.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


FIXTURE = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__hold10003 = true; window.__held10003 = 0;
const __bs = WebSocket.prototype.send;
WebSocket.prototype.send = function(r){
  try{ const m = JSON.parse(r);
    if(window.__hold10003 && m[0] === 'REQ' && m.slice(2).some(f => (f.kinds || []).includes(10003))){ __held10003++; return; }
  }catch(_){}
  return __bs.call(this, r);
};
'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_bookmarks_read_the_list_again_when_the_boot_read_was_missed():
    async def check(b):
        await desktop.login(b)
        ids = await b.js(r"""(() => {
          const me = __PC.me().pubkey, now = Math.floor(Date.now() / 1000), other = '7'.repeat(64);
          const note = (id, text) => ({ id, kind: 1, pubkey: other, created_at: now - 300, sig: 'a'.repeat(128), content: text, tags: [] });
          const a = note('a'.repeat(64), 'Sourdough starter notes'), b2 = note('b'.repeat(64), 'Hydration chart');
          const list = (id, t, es) => ({ id, kind: 10003, pubkey: me, created_at: now - t, sig: 'c'.repeat(128), content: '',
                                        tags: es.map(e => ['e', e]) });
          const oldList = list('d'.repeat(64), 100, [a.id]), newList = list('e'.repeat(64), 10, [a.id, b2.id]);
          [a, b2, oldList].forEach(e => Store.saveEvent(e));           // this device: the older list
          (window.__events = window.__events || []).push(a, b2, newList); // the relay: the newer one
          return [a.id, b2.id];
        })()""")
        # The boot's read of the list must have been the one that went unanswered -- or this proves nothing.
        await b.until("__held10003 >= 1")
        await asyncio.sleep(7)                                          # past Relay.query's 6 s: it gave up
        await b.js("window.__hold10003 = false; __PC.switchView('bookmarks'); 1")
        listed = "[...document.querySelectorAll('#feed .note[data-id]')].map(x=>x.dataset.id)"
        for _ in range(100):
            got = await b.js(listed)
            if all(i in got for i in ids):
                break
            await asyncio.sleep(.2)
        assert all(i in got for i in ids), ("Bookmarks kept this device's old list after a missed read", got)
        assert not await b.js("__errors.slice()")

    asyncio.run(desktop.with_browser('online', '', check, FIXTURE))
