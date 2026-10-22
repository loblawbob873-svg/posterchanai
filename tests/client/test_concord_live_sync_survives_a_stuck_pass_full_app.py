"""ONE LIVE PASS THAT NEVER ENDS MUST NOT END LIVE SYNC (2026-10-22, the Windows app: "not syncing latest room data",
new messages missing while it was open, only a restart brought them back).

refreshActiveChannel re-arms the open channel's live subscription and polls it, and it ran only while `liveBusy` was
false. An await in it that never settles -- the IndexedDB write here, which has no timeout -- held that flag for the
life of the page, so every later tick returned at once. Real login, Relay, DOM and concord.js; the cache write is made
to hang, and the clock is moved past the stuck limit instead of waiting 90 s.
"""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_pass_stuck_on_a_cache_write_is_replaced_and_named():
    async def check(b):
        await desktop.login(b)
        await b.js(r'''(()=>{
          const key=new Uint8Array(32).fill(6),pk=NostrTools.getPublicKey(key);
          const channels=[{id:'stuck-general',name:'general',streamPubkeys:[pk]}];
          const room={name:'Stuck fixture',communityId:'e'.repeat(64),channels,cord:{bundle:{relays:['wss://stuck.fixture.invalid']},hydrated:true}};
          localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
          window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels}),
            inspectChat:async()=>({messages:[],reactions:[],reactionUrls:[]})};
          window.__queries=0;
          const query=__PC.relayQueryFrom;
          const wrap=NostrTools.finalizeEvent({kind:1059,created_at:Math.floor(Date.now()/1000),content:'hi',tags:[]},key);
          __PC.relayQueryFrom=(urls,filters,opts)=>{ if(!filters.some(f=>f.kinds?.includes(1059)))return query(urls,filters,opts);
            __queries++; const out=[wrap]; Object.defineProperty(out,'complete',{value:true}); return Promise.resolve(out); };
          __PC.switchMessagesTab('concord');
        })()''')
        await b.until("!!document.querySelector('#cc-input')")
        await b.js(r'''(()=>{
          PCConcord.__testState({controls:['e'.repeat(64),[{id:'stuck-control'}]]});
          window.PCConcordCache=window.PCConcordCache||{};
          PCConcordCache.put=()=>new Promise(()=>{});          // an IndexedDB write that never settles
          void PCConcord.__testRefreshActiveChannel(__PC);
        })()''')
        await b.until("PCConcord.liveDiag().busy && PCConcord.liveDiag().step==='cache'")
        before = await b.js('__queries')
        await b.js('void PCConcord.__testRefreshActiveChannel(__PC)')
        await asyncio.sleep(.5)
        assert await b.js("PCConcord.liveDiag().step") == 'cache', 'a stuck pass was replaced before the limit'
        assert await b.js('__queries') == before, 'a second pass ran while the first was still inside the limit'
        await b.js("(()=>{const real=Date.now.bind(Date);Date.now=()=>real()+100000;})()")   # 100 s later
        await b.js('void PCConcord.__testRefreshActiveChannel(__PC)')
        await b.until('PCConcord.liveDiag().stuck.length>=1')
        stuck = await b.js('PCConcord.liveDiag().stuck[0]')
        assert stuck['step'] == 'cache' and stuck['forMs'] >= 90000, stuck
        await b.until('__queries>%d' % before)          # the fresh pass asked the room's relays again
    extra = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
'''
    asyncio.run(desktop.with_browser('online', '', check, extra))
