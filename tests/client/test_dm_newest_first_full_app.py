"""MESSAGES PAINTS THE LATEST DM WITH THE REST, AND NOTHING IN THE HISTORY CAN STALL THE QUEUE.

Reported as "Messages is not caching that well. It quickly loads a batch of old messages and takes
forever to load the latest." A returning device already holds its history in the decrypted-DM cache
(`pc-dm-v1`); only what arrived since the last visit needs a decrypt. Three things held exactly that
back, each measured here against the SHIPPED bundle on a reload with a warm cache:

  cord     a Concord invite (`k 3313`) never enters `_wrapTried`, so the history drain read it as a
           failed signer and paused EVERYTHING for 30s — with nothing to resume it but the 60s
           watcher. One invite at the head of the queue held the whole history at a handful.
  bad      the same pause for one wrap that will not decrypt, on a LOCAL key, where a throw is about
           that wrap and never about a signer being down.
  slow     the whole history waited on pullShared — a pointer read, a blob and a re-write of every
           record the device already held — before even a free cache hit could paint. Over a 1.5s
           relay: 3500ms from opening Messages to the latest DM before, 1880ms after, where the
           relay's own answer accounts for ~1.6s of it.

Only the relay socket and the Blossom blob are fixtures; the relay keeps its events across the
reload, so the second visit is a real warm start.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

N_OLD = 600


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


RELAY = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__RELAY_MS=+(localStorage.getItem('__relayMs')||100);
window.__t0=performance.now();
window._rel=()=>{try{return JSON.parse(localStorage.getItem('__relayEvents')||'[]')}catch(_){return[]}};
const _match=(f,ev)=>{
 if(f.kinds&&!f.kinds.includes(ev.kind))return false;
 if(f.ids&&!f.ids.includes(ev.id))return false;
 if(f.authors&&!f.authors.includes(ev.pubkey))return false;
 if(f.until!=null&&ev.created_at>f.until)return false;
 if(f.since!=null&&ev.created_at<f.since)return false;
 for(const k of Object.keys(f))if(k[0]==='#'){if(!(ev.tags||[]).some(t=>t[0]===k.slice(1)&&f[k].includes(t[1])))return false;}
 return true;};
class RelaySocket extends EventTarget{
 static OPEN=1;static CONNECTING=0;static CLOSING=2;static CLOSED=3;
 constructor(url){super();this.url=String(url);this.readyState=0;__sockets.push(this);setTimeout(()=>{this.readyState=1;this.fire('open',{});},10)}
 fire(type,data){const e=type==='message'?new MessageEvent(type,{data:JSON.stringify(data)}):new Event(type);this['on'+type]?.(e);this.dispatchEvent(e)}
 send(raw){const m=JSON.parse(raw);if(!Array.isArray(m))return;
  if(m[0]==='REQ'){const sub=m[1],fs=m.slice(2);
   setTimeout(()=>{const all=_rel().sort((a,b)=>b.created_at-a.created_at);const out=new Map();
    for(const f of fs){let n=0;const lim=Math.min(f.limit??500,500);for(const ev of all){if(n>=lim)break;if(_match(f,ev)){out.set(ev.id,ev);n++;}}}
    for(const ev of out.values())this.fire('message',['EVENT',sub,ev]);this.fire('message',['EOSE',sub]);},__RELAY_MS);}
  if(m[0]==='EVENT'){__published.push(m[1]);const r=_rel();if(!r.some(e=>e.id===m[1].id)){r.push(m[1]);localStorage.setItem('__relayEvents',JSON.stringify(r));}
   setTimeout(()=>this.fire('message',['OK',m[1].id,true,'ok']),__RELAY_MS);}
 }
 close(){this.readyState=3;this.fire('close',{})}
}
window.WebSocket=RelaySocket;
{const f0=window.fetch;window.fetch=function(url){const sha=sessionStorage.getItem('__blobSha');
 if(sha&&String(url).includes(sha))return Promise.resolve(new Response(Uint8Array.from(atob(sessionStorage.getItem('__blob')),c=>c.charCodeAt(0)),{status:200}));
 return f0.apply(this,arguments);};}
'''

# Real NIP-17 wraps, stamped when they were sent and back-dated up to two days, as NIP-59 says.
SEED = r'''((phase,n)=>{
 const me=new Uint8Array(32).fill(1),mePk=NostrTools.getPublicKey(me),now=Math.floor(Date.now()/1000);
 const {createRumor,createSeal}=NostrTools.nip59;
 const wrap=(sk,t,text)=>{const seal=createSeal(createRumor({kind:14,created_at:t,tags:[['p',mePk]],content:text},sk),sk,mePk);
   const eph=NostrTools.generateSecretKey();
   return NostrTools.finalizeEvent({kind:1059,created_at:t-Math.floor(Math.random()*2*86400),tags:[['p',mePk]],
     content:NostrTools.nip44.encrypt(JSON.stringify(seal),NostrTools.nip44.getConversationKey(eph,mePk))},eph);};
 const junk=tags=>NostrTools.finalizeEvent({kind:1059,created_at:now-5,tags:[['p',mePk],...tags],content:'AgAAAA'},NostrTools.generateSecretKey());
 const out=[];
 if(phase==='old')for(let i=0;i<n;i++)out.push(wrap(new Uint8Array(32).fill(10+(i%20)),now-5*86400-i*2000,'old '+i));
 if(phase==='cord')out.push(junk([['k','3313']]));
 if(phase==='bad')out.push(junk([]));
 if(phase==='new')for(let i=0;i<n;i++)out.push(wrap(new Uint8Array(32).fill(99),now-120+i,'NEWEST '+i));
 const r=_rel();for(const e of out)r.push(e);localStorage.setItem('__relayEvents',JSON.stringify(r));return out.length;})'''

# The shared cache, as another device's pushShared publishes it.
SHARE = r'''(async()=>{
 const me=new Uint8Array(32).fill(1),mePk=NostrTools.getPublicKey(me),rel=_rel();
 const kd=rel.find(e=>e.kind===30078&&e.tags.some(t=>t[0]==='d'&&t[1]==='pcai:dmkey'));
 const hex=NostrTools.nip44.decrypt(kd.content,NostrTools.nip44.getConversationKey(me,mePk));
 const key=await crypto.subtle.importKey('raw',new Uint8Array(hex.match(/../g).map(h=>parseInt(h,16))),{name:'AES-GCM'},false,['encrypt']);
 const map={};for(const w of rel)if(w.kind===1059)map[w.id]=NostrTools.nip59.unwrapEvent(w,me);
 const iv=crypto.getRandomValues(new Uint8Array(12));
 const ct=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv},key,new TextEncoder().encode(JSON.stringify(map))));
 const body=new Uint8Array(12+ct.length);body.set(iv,0);body.set(ct,12);
 const sha=[...new Uint8Array(await crypto.subtle.digest('SHA-256',body))].map(x=>x.toString(16).padStart(2,'0')).join('');
 let s='';for(let i=0;i<body.length;i+=8192)s+=String.fromCharCode(...body.subarray(i,i+8192));
 sessionStorage.setItem('__blob',btoa(s));sessionStorage.setItem('__blobSha',sha);
 rel.push(NostrTools.finalizeEvent({kind:30078,created_at:Math.floor(Date.now()/1000),tags:[['d','pcai:dmcache']],
   content:JSON.stringify({sha,n:Object.keys(map).length,at:1})},me));
 localStorage.setItem('__relayEvents',JSON.stringify(rel));})()'''

# When each conversation row first appears, and when the newest one first shows its latest message.
SAMPLE = r'''(newpk=>{window.__seen={};window.__newAt=null;setInterval(()=>{
 const t=Math.round(performance.now()-__t0);
 for(const r of document.querySelectorAll('#dm-rows .dm-peer'))if(!(r.dataset.peer in __seen))__seen[r.dataset.peer]=t;
 const row=document.querySelector('#dm-rows .dm-peer[data-peer="'+newpk+'"]');
 if(__newAt==null&&row&&row.textContent.includes('NEWEST 2'))__newAt=t;},20);})'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('case', ['cord', 'bad', 'slow'])
def test_latest_dm_paints_with_the_cached_history(case):
    async def check(b):
        await b.js("localStorage.setItem('__relayMs'," + ('1500' if case == 'slow' else '100') + ")")
        await desktop.login(b)
        await b.js(SEED + "('old'," + str(N_OLD) + ")")
        await b.js("__PC.switchView('messages')")
        await b.until("__PC.dmStats().done>=" + str(N_OLD))
        await asyncio.sleep(1.5)                                   # let the cache writes settle
        if case == 'slow':
            await b.js(SHARE)
        else:
            await b.js(SEED + "('" + case + "',1)")
        await b.js(SEED + "('new',3)")
        newpk = await b.js("NostrTools.getPublicKey(new Uint8Array(32).fill(99))")
        await b.call('Page.reload')
        await asyncio.sleep(.2)
        await b.until('!!window.__PC && !!__PC.me() && document.readyState==="complete"')
        await b.js(SAMPLE + '(' + json.dumps(newpk) + ')')
        await b.js("window.__openAt=Math.round(performance.now()-__t0);__PC.switchView('messages')")
        for _ in range(100):                                       # 10s; the stall was 30s and more
            if await b.js('__newAt!=null && __PC.dmStats().done>=' + str(N_OLD + 3)):
                break
            await asyncio.sleep(.1)
        state = await b.js("({openAt:__openAt,newAt:__newAt,rows:Object.keys(__seen).length,stats:__PC.dmStats(),errors:__errors})")
        assert state['newAt'] is not None, state
        assert state['stats']['done'] >= N_OLD + 3, ('the history stalled', state)
        assert state['rows'] == 21, state
        first = min((await b.js('__seen')).values())
        # The latest conversation is painted WITH the cached ones, not seconds behind them.
        assert state['newAt'] - first <= 600, ('latest DM painted after the cached history', first, state)
        if case == 'slow':
            assert state['newAt'] - state['openAt'] <= 2600, ('Messages waited on the shared cache', state)
        print(case, 'open→first row', first - state['openAt'], 'ms, open→latest', state['newAt'] - state['openAt'], 'ms')

    asyncio.run(desktop.with_browser('online', '', check, RELAY))
