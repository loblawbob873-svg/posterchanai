"""A PROFILE'S FOLLOWER AND POST COUNTS ARE A RELAY'S ANSWER, OR SAY THEY ARE UNKNOWN -- NEVER AN INVENTED 0.

Reported from an iPhone 12 (Safari and Brave), 2026-10-30: "The followers glitches a lot and doesn't show the correct
number." Safari drops a backgrounded tab's sockets; a NIP-45 COUNT written into one is never answered, and Relay.count
resolved that silence as 0, which the profile painted as the number. The shipped client against a stub relay (at the
WebSocket, so the real Relay.count and profile code run): one that answers COUNT with 42, and one that never answers it.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')

INIT = r'''
const __SK2 = new Uint8Array(32).fill(2);
function __other(){
  const T = window.NostrTools, pk = T.getPublicKey(__SK2), now = Math.floor(Date.now()/1000);
  window.__OTHER = pk;
  return { pk, k0: T.finalizeEvent({ kind:0, created_at:now-60, tags:[], content:JSON.stringify({ name:'Counted' }) }, __SK2),
           k3: T.finalizeEvent({ kind:3, created_at:now-50, tags:[['p','a'.repeat(64)],['p','b'.repeat(64)]], content:'' }, __SK2) };
}
class FS{constructor(u){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen&&this.onopen();},20);}
 send(raw){let m;try{m=JSON.parse(raw);}catch(_){return;}
  const out=o=>{if(this.readyState===1&&this.onmessage)this.onmessage({data:JSON.stringify(o)});};
  if(m[0]==='EVENT'){ out(['OK', m[1].id, true, '']); return; }
  if(m[0]==='COUNT'){ if(window.__countAnswer!=null) out(['COUNT', m[1], {count: window.__countAnswer}]); return; }   // null: silent
  if(m[0]!=='REQ') return; const id=m[1];
  let O=null; try{ O=__other(); }catch(_){}
  for(const f of m.slice(2)) if(O && (f.authors||[]).includes(O.pk)){
    if((f.kinds||[]).includes(0)) out(['EVENT',id,O.k0]); if((f.kinds||[]).includes(3)) out(['EVENT',id,O.k3]); }
  out(['EOSE',id]); }
 close(){this.readyState=3;} }
FS.OPEN=1;FS.CONNECTING=0;FS.CLOSED=3;window.WebSocket=FS;
'''

STATS = r'''(()=>{const t=s=>((document.querySelector(s+' b')||{}).textContent||'').trim();
  return {followers:t('#show-followers'), following:t('#show-following'), posts:t('#show-posts')};})()'''


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@CHROME
@pytest.mark.parametrize('answer', [42, None])
def test_follower_and_post_counts_are_the_relays_answer_or_unknown(answer):
    got = {}

    async def check(b):
        await b.js("window.__countAnswer = %s; true" % ('null' if answer is None else answer))
        await desktop.login(b)
        await b.js("try{ __other(); if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}; true")
        await b.until("!!window.__OTHER")
        await b.js("__PC.openProfile(window.__OTHER)")
        await b.until("!!document.querySelector('#show-followers')")
        # the stats loop retries up to three times with a back-off before it writes its final values
        for _ in range(120):   # up to 3 attempts x (4 s COUNT timeout + back-off) before the final write
            s = await b.js(STATS)
            if s['followers'] not in ('', '·') and s['posts'] not in ('', '·'):
                break
            await asyncio.sleep(.25)
        await asyncio.sleep(1)
        got['s'] = await b.js(STATS)

    asyncio.run(desktop.with_browser('online', '', check, INIT))
    s = got['s']
    assert s['following'] == '2', ('the contact list (2 people) was answered and must show', s)
    if answer is None:
        assert s['followers'] == '–' and s['posts'] == '–', ('a COUNT nobody answered was shown as a number', s)
    else:
        assert s['followers'] == '42' and s['posts'] == '42', s
