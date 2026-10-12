"""PosterChanOS: the desktop paints from its LOCAL RELAY, and falls back to IndexedDB when it is down.

The real bundled client (build-www.sh) in Chrome, with the desktop preload's `pcShell.localRelay` pointing
at the SHIPPED local relay process (`relay_main.py --os-mode`, a real loopback socket). Every other network
boundary is the usual fixture. One browser profile, three boots:

  A. relay UP, the network has NOTHING: the Global timeline paints the posts only the local relay holds,
     the Store reports backend 'relay', and IndexedDB received no timeline events.
  B. relay DOWN (process stopped), the network has posts: the timeline paints them, the Store reports
     backend 'idb', and it wrote them to IndexedDB -- the fallback is the old cache, working.
  C. relay still down AND the network dead: the timeline still paints those posts, from IndexedDB alone.
     A desktop whose cache service did not start is never a blank timeline.
"""
import asyncio
import shutil
import time
from pathlib import Path

import pytest

from tests import os_relay_harness as H
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PRE = "window.__NativeWS = window.WebSocket;"

INIT = r'''
(()=>{const N=window.__NativeWS, Fx=window.WebSocket;
  class Dead extends EventTarget{static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
    constructor(u){super();this.url=String(u);this.readyState=0;} send(){} close(){this.readyState=3;}}
  const netDead=localStorage.getItem('__netdead')==='1';
  function W(u,p){const s=String(u); if(s.startsWith('ws://127.0.0.1:')) return new N(s,p); return netDead?new Dead(s):new Fx(s);}
  W.OPEN=1;W.CONNECTING=0;W.CLOSING=2;W.CLOSED=3; window.WebSocket=W;
  window.pcShell.localRelay=()=>'LOCALRELAY';
  let built=null;
  Object.defineProperty(window,'__events',{configurable:true,set(v){built=v;},get(){
    if(built) return built; if(!window.NostrTools) return [];
    if(localStorage.getItem('__phase')!=='B') return built=[];
    const k=new Uint8Array(32).fill(9);
    return built=Array.from({length:6},(_,i)=>NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000)-100-i,content:'from the network '+i,tags:[]},k));}});
})();
'''

FEED = "(document.querySelector('#feed')||{}).innerText||''"
IDB_EVENTS = r'''new Promise(res=>{const r=indexedDB.open('posterchan-nostr');r.onsuccess=()=>{const db=r.result;
  const q=db.transaction('events','readonly').objectStore('events').getAll();q.onsuccess=()=>res(q.result.filter(e=>e.kind===1).map(e=>e.content));q.onerror=()=>res(['ERR'])};r.onerror=()=>res(['ERR'])})'''


async def _feed_has(b, needle, tries=150):
    for _ in range(tries):
        if needle in await b.js(FEED):
            return True
        await asyncio.sleep(0.1)
    return False


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_timeline_paints_from_the_local_relay_and_from_indexeddb_without_it():
    tmp = H.tmpdir()
    relay = H.LocalRelay(tmp)
    got = {}
    try:
        relay.start()

        async def seed():
            async with H.Client(relay.url) as c:
                for i in range(5):
                    ok, why = await c.publish(H.ev(bytes([5] * 32), 1, "from the local relay %d" % i,
                                                   created_at=int(time.time()) - 60 - i))
                    assert ok, why
        asyncio.run(seed())

        async def check(b):
            await desktop.login(b)
            await b.js("__PC.switchView('global')")
            got['A_painted'] = await _feed_has(b, 'from the local relay')
            got['A_backend'] = await b.js("Store.backend()")
            await asyncio.sleep(1.5)
            got['A_idb'] = await b.js(IDB_EVENTS)
            # B: the relay goes away, the network has posts.
            relay.stop()
            await b.js("localStorage.setItem('__phase','B')")
            await b.call('Page.reload')
            await b.until('!!window.__PC && !!window.PCOS')
            await b.until("!!__PC.me()")
            await b.js("__PC.switchView('global')")
            got['B_painted'] = await _feed_has(b, 'from the network')
            got['B_backend'] = await b.js("Store.backend()")
            await asyncio.sleep(1.5)
            got['B_stats'] = await b.js("Store.backendStats()")
            # C: no relay, no network: IndexedDB alone.
            await b.js("localStorage.setItem('__netdead','1')")
            await b.call('Page.reload')
            await b.until('!!window.__PC && !!window.PCOS')
            await b.until("!!__PC.me()")
            await b.js("__PC.switchView('global')")
            got['C_painted'] = await _feed_has(b, 'from the network')
            got['C_feed'] = (await b.js(FEED))[:400]

        asyncio.run(desktop.with_browser('online', '', check, INIT.replace('LOCALRELAY', relay.url), pre_init=PRE))
    finally:
        relay.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    assert got.get('A_painted'), ('the timeline did not paint what the local relay holds', got)
    assert got['A_backend'] == 'relay', got
    assert not any('from the local relay' in c for c in (got.get('A_idb') or [])), \
        ('with the local relay up, timeline events still went into IndexedDB', got.get('A_idb'))
    assert got.get('B_painted') and got['B_backend'] == 'idb', got
    assert got['B_stats']['idb'] > 0, ('the fallback wrote nothing to IndexedDB', got['B_stats'])
    assert got.get('C_painted'), ('with the relay and the network gone the timeline was blank', got.get('C_feed'))
