"""Expired Concord history disappears from rendered UI and actual IndexedDB."""
import asyncio
from pathlib import Path
import pytest
from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_expiration_purges_verified_ciphertext_and_visible_history():
    async def check(b):
        await desktop.login(b)
        await b.js(r'''(()=>{
          const room={name:'Expiry fixture',communityId:'c'.repeat(64),naddr:'expiry-fixture',channels:[{id:'fixture-general',name:'general'}],cord:{bundle:{relays:['wss://fixture.invalid']},hydrated:true}};
          localStorage.setItem('pc.concord.invites',JSON.stringify([room]));localStorage.setItem('pc.concord.active','0');
          window.__expiryClock=Date.now();Date.now=()=>window.__expiryClock;
          window.__deadline=Math.floor(Date.now()/1000)+60;
          window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'fixture-general',name:'general',streamPubkeys:[]}]}),
            inspectChat:async()=>({messages:[{id:'expiring-rumor',pubkey:__PC.me().pubkey,text:'Disappearing fixture message',kind:9,at:Date.now(),tags:[['expiration',String(__deadline)]]},{id:'permanent-rumor',pubkey:__PC.me().pubkey,text:'Permanent fixture message',kind:9,at:Date.now(),tags:[]}],reactions:[],reactionIds:[]})};
          __PC.switchMessagesTab('concord');
        })()''')
        await b.until("document.querySelector('.cc-messages')?.textContent.includes('Disappearing fixture message')")
        await b.js(r'''(async()=>{
          const C=PCConcordCache,stream='expiry-browser-test',ev=(id,tags=[])=>({id,kind:1059,created_at:Math.floor(Date.now()/1000),content:'ciphertext-'+id,tags});
          window.__expiryStream=stream;await C.put(stream,[ev('signed-expiry'),ev('outer-only',[['expiration','1']]),ev('control-no-expiry')]);
          await C.expireEvents(stream,[['signed-expiry',__deadline]]);
          // Repeated receipt must not erase the authenticated deadline stored beside ciphertext.
          await C.put(stream,[ev('signed-expiry')]);
          await C.putDelivery(stream,ev('pending-expiry'));
          await C.expireEvents(stream,[['pending-expiry',__deadline]]);
          await C.putDelivery(stream,ev('ack-expiry'));
          await C.expireEvents(stream,[['ack-expiry',__deadline]]);
          await C.completeDelivery(stream,'ack-expiry',stream);
        })()''')
        assert await b.js("(async()=> (await PCConcordCache.get(__expiryStream)).length)()") == 4
        await b.js("window.__expiryClock=(__deadline+1)*1000")
        await b.until("!document.querySelector('.cc-messages')?.textContent.includes('Disappearing fixture message')")
        assert await b.js("document.querySelector('.cc-messages').textContent.includes('Permanent fixture message')")
        assert await b.js("(async()=> (await PCConcordCache.getDeliveries(__expiryStream)).length)()") == 0
        # Inspect the underlying store, not a filtered get() result: plaintext hiding alone is insufficient.
        await b.until("(async()=>{const db=await new Promise((r,j)=>{const q=indexedDB.open(PCConcordCache.DB);q.onsuccess=()=>r(q.result);q.onerror=j;});const rows=await new Promise((r,j)=>{const q=db.transaction(PCConcordCache.STORE).objectStore(PCConcordCache.STORE).getAll();q.onsuccess=()=>r(q.result);q.onerror=j;});db.close();return !rows.some(r=>r.stream===__expiryStream&&r.id==='signed-expiry');})()")
        assert await b.js("(async()=> (await PCConcordCache.page(__expiryStream)).events.map(e=>e.id).sort())()") == ['control-no-expiry','outer-only']
    extra="localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_expiry_index_upgrade_preserves_existing_encrypted_history():
    async def check(b):
        result = await b.js(r'''(async()=>{
          const C=PCConcordCache;
          const old=await new Promise((resolve,reject)=>{const q=indexedDB.open(C.DB,4);q.onupgradeneeded=()=>{const db=q.result,s=db.createObjectStore(C.STORE,{keyPath:'key'});s.createIndex('stream','stream');s.createIndex('streamCreated',['stream','created']);db.createObjectStore(C.ICONS,{keyPath:'key'});db.createObjectStore(C.DELIVERIES,{keyPath:'key'});};q.onsuccess=()=>resolve(q.result);q.onerror=()=>reject(q.error);});
          await new Promise((resolve,reject)=>{const tx=old.transaction(C.STORE,'readwrite');tx.objectStore(C.STORE).put({key:'legacy\u0000message',stream:'legacy',id:'message',created:1,size:20,event:{id:'message',kind:1059,created_at:1,content:'old encrypted history',tags:[]}});tx.oncomplete=resolve;tx.onerror=reject;});old.close();
          const history=await C.get('legacy');
          const db=await new Promise((r,j)=>{const q=indexedDB.open(C.DB);q.onsuccess=()=>r(q.result);q.onerror=()=>j(q.error);});
          const tx=db.transaction([C.STORE,C.DELIVERIES]),result={version:db.version,history,historyIndex:tx.objectStore(C.STORE).indexNames.contains('expires'),pendingIndex:tx.objectStore(C.DELIVERIES).indexNames.contains('expires')};db.close();return result;
        })()''')
        assert result['version'] == 5
        assert result['historyIndex'] and result['pendingIndex']
        assert [e['content'] for e in result['history']] == ['old encrypted history']
    asyncio.run(desktop.with_browser('online', '', check))
