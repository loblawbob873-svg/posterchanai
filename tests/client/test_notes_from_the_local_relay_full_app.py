"""PosterChanOS: with the instance unreachable, Notes shows EVERY note the desktop's local relay holds.

Reported 2026-10-14: "when the outage happened, notes on desktop did not show all my notes". The desktop's
local relay downloads the account's private kind-30078 documents from the instance in the background
(osmode.py's "private" scope), so it holds notes this client never saved through its own Store -- written
on a phone, synced while Notes was closed. The real bundled client in Chrome, the SHIPPED local relay
process, and a dead network: the notes are planted in the local relay only (exactly where the background
sync puts them), the page reloads with every non-loopback socket dead, and Notes is opened.
"""
import asyncio
import json
import shutil

import pytest
from pathlib import Path

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
})();
'''

N_NOTES = 30

# Signed and encrypted IN the page with the signed-in key, then written straight onto the local relay's
# socket -- never through Store, so this client's IndexedDB has never seen them (the background-sync shape).
PLANT = r'''(async()=>{
  const me=__PC.me().pubkey, sk=new Uint8Array(32).fill(1), now=Math.floor(Date.now()/1000), out=[];
  for(let i=0;i<N;i++){
    const ct=await __PC.nip44enc(me, JSON.stringify({title:'synced note '+i, body:'body '+i, tags:[], res:[], updated:now-i}));
    out.push(NostrTools.finalizeEvent({kind:30078, created_at:now-3600-i, content:ct,
      tags:[['d','pcai:note:synced'+i],['l','pcai-notes']]}, sk));
  }
  return JSON.stringify(out);
})()'''.replace('N;', '%d;' % N_NOTES)

COUNT = "document.querySelectorAll('.nt-item').length"


WINDOW = "window.pcShell.windowContext={role:'app',view:'notes'};window.pcShell.backgroundOwner=false;"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('shell', ['desktop-page', 'notes-window'])
def test_notes_shows_every_note_the_local_relay_holds_while_the_instance_is_down(shell):
    """On PosterChanOS Notes is its OWN native window -- a separate page with its own Store and its own
    sockets -- so it is run both ways."""
    tmp = H.tmpdir()
    relay = H.LocalRelay(tmp)
    got = {}
    try:
        relay.start()

        async def check(b):
            await desktop.login(b)
            evs = json.loads(await b.js(PLANT))

            async def seed():
                async with H.Client(relay.url) as c:
                    for ev in evs:
                        ok, why = await c.publish(ev)
                        assert ok, why
            await seed()
            got['idb_before'] = await b.js(r'''new Promise(res=>{const r=indexedDB.open('posterchan-nostr');r.onsuccess=()=>{
              const q=r.result.transaction('events','readonly').objectStore('events').getAll();
              q.onsuccess=()=>res(q.result.filter(e=>e.kind===30078).length);q.onerror=()=>res(-1)};r.onerror=()=>res(-1)})''')
            # The outage: every socket that is not the local relay is dead from the next boot on.
            await b.js("localStorage.setItem('__netdead','1')")
            await b.call('Page.reload')
            await b.until('!!window.__PC && !!window.PCOS')
            await b.until("!!__PC.me()")
            got['oswin'] = await b.js("document.documentElement.classList.contains('pc-oswin')")
            await b.js("__PC.switchView('notes')")
            n = 0
            for _ in range(150):
                n = await b.js(COUNT)
                if n >= N_NOTES:
                    break
                await asyncio.sleep(0.1)
            got['shown'] = n
            got['text'] = (await b.js("(document.querySelector('#feed')||{}).innerText||''"))[:300]

        init = INIT.replace('LOCALRELAY', relay.url)
        if shell == 'notes-window':
            init = init.replace("window.pcShell.localRelay=", "if(localStorage.getItem('__netdead')==='1'){" + WINDOW + "}window.pcShell.localRelay=")
        asyncio.run(desktop.with_browser('online', '', check, init, pre_init=PRE))
    finally:
        relay.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    assert got.get('oswin') == (shell == 'notes-window'), ('did not run in the shell it claims', shell, got)
    assert got.get('idb_before') == 0, ('the planted notes reached IndexedDB; the test no longer models a '
                                        'background sync', got)
    assert got.get('shown') == N_NOTES, ('with the instance down, Notes showed %r of the %d notes the local '
                                         'relay holds' % (got.get('shown'), N_NOTES), got)
