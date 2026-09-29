"""The real bundled client boots with dms.js split out of app.js, and a DM arrives and opens.

Drives the SHIPPED desktop bundle (desktop/build-www.sh renders templates/client.html and copies
every client script) in headless Chrome, through the same harness as the offline desktop test and
the same in-page relay as the newest-first DM test. Only the relay socket is a fixture; the gift
wraps are REAL NIP-17 (rumor → seal → wrap, signed by a second key), and everything that receives,
unwraps, lists, notifies and opens them is the shipped code. What a broken split looks like:

  * dms.js loaded after app.js (or missing) -> the factory is built later, by whichever call reaches
    a forwarder first (or fetched again by the lazy loader) — caught by WHERE it was built: once,
    during app.js's own evaluation, from `_dmsMod`, never from `_lzRun`;
  * a dependency not passed, or a live binding captured by value (`S._dmLoaded`, `S._dmUnread`,
    `S.ME`, `S.signer`) -> the history never lists, the badge never counts, or a ReferenceError;
  * the DmCache proxy not reaching the module's object -> the logout path's `DmCache.forget` throws;
  * and no page error or console error anywhere.

Two deliveries, because they are two code paths: the HISTORY read when Messages opens (ensureDMs →
the queue → ingestWrap), and a LIVE wrap pushed on the ungated 1059 subscription after its EOSE,
which must raise the unread badge and the DM notification (_dmNotify) while another view is showing.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


BUILD_PROBE = r'''
window.__consoleErrors=[];
{const ce=console.error.bind(console);console.error=(...a)=>{__consoleErrors.push(a.map(x=>String(x&&x.stack||x)).join(' ').slice(0,400));ce(...a);};}
addEventListener('unhandledrejection',e=>__consoleErrors.push('unhandled: '+String(e.reason&&e.reason.stack||e.reason).slice(0,400)));
{let real;Object.defineProperty(window,'PCDmsFactory',{configurable:true,get(){return real;},set(f){
  real=function(dep){
    window.__dmBuilds=(window.__dmBuilds||0)+1;
    window.__dmBuiltIn=(document.currentScript&&document.currentScript.src||'').split('?')[0].split('/').pop();
    window.__dmBuiltStack=String(new Error().stack);
    window.__dmBuiltBeforePC=!window.__PC;
    return f.apply(this,arguments);
  };}});}
// Every REQ the client writes, so the test can push a LIVE event down the subscription it opened.
window.__reqs=[];
{const send=RelaySocket.prototype.send;RelaySocket.prototype.send=function(raw){
  try{const m=JSON.parse(raw);if(Array.isArray(m)&&m[0]==='REQ')__reqs.push({sock:this,sub:m[1],fs:m.slice(2)});}catch(_){}
  return send.apply(this,arguments);};}
'''

# A real NIP-17 DM from `from` (a 32-byte fill) to the logged-in key (fill 1), as a gift wrap.
WRAP = r'''((from,text,ago)=>{
 const me=new Uint8Array(32).fill(1),mePk=NostrTools.getPublicKey(me),now=Math.floor(Date.now()/1000);
 const sk=new Uint8Array(32).fill(from);
 const seal=NostrTools.nip59.createSeal(NostrTools.nip59.createRumor({kind:14,created_at:now-ago,tags:[['p',mePk]],content:text},sk),sk,mePk);
 const eph=NostrTools.generateSecretKey();
 return NostrTools.finalizeEvent({kind:1059,created_at:now-ago-3600,tags:[['p',mePk]],
   content:NostrTools.nip44.encrypt(JSON.stringify(seal),NostrTools.nip44.getConversationKey(eph,mePk))},eph);})'''


async def _boot_receive_and_open(b):
    order = await b.js("[...document.scripts].map(s=>(s.getAttribute('src')||'').split('?')[0].split('/').pop())"
                       ".filter(n=>n==='dms.js'||n==='app.js')")
    assert order == ['dms.js', 'app.js'], order
    assert await b.js("window.__dmBuilds") == 1
    assert await b.js("window.__dmBuiltIn") == 'app.js'
    assert await b.js("window.__dmBuiltBeforePC") is True
    stack = await b.js("window.__dmBuiltStack")
    assert '_dmsMod' in stack and '_lzRun' not in stack, stack

    await desktop.login(b)
    alice = await b.js("NostrTools.getPublicKey(new Uint8Array(32).fill(7))")
    bob = await b.js("NostrTools.getPublicKey(new Uint8Array(32).fill(8))")

    # 1. HISTORY: a DM already on the relay lists when Messages opens, and opens into its thread.
    await b.js("(()=>{const r=_rel();r.push(" + WRAP + "(7,'HELLO FROM THE HISTORY',600));"
               "localStorage.setItem('__relayEvents',JSON.stringify(r));})()")
    await b.js("__PC.switchView('messages')")
    row = f"#dm-rows .dm-peer[data-peer=\"{alice}\"]"
    await b.until(f"!!document.querySelector({json.dumps(row)})")
    await b.js(f"document.querySelector({json.dumps(row)}).click()")
    await b.until("(document.querySelector('#dm-thread')||{}).innerText && "
                  "document.querySelector('#dm-thread').innerText.includes('HELLO FROM THE HISTORY')")

    # 2. LIVE: leave Messages, then push a new wrap down the ungated live 1059 subscription (the one
    #    with no `limit` and no Concord `#k`) after its EOSE — the badge counts it and _dmNotify fires.
    await b.js("__PC.switchView('home')")
    # Opening Messages marked everything up to that second as read (`dmSeen`); a new message is newer.
    await asyncio.sleep(1.2)
    await b.until("__reqs.some(r=>r.fs.some(f=>(f.kinds||[]).includes(1059)&&f.limit==null&&!f['#k']))")
    await b.js("window.__toasts=[];{const o=document.body;new MutationObserver(()=>{"
               "document.querySelectorAll('.notif-toast,.toast').forEach(t=>__toasts.push(t.innerText));})"
               ".observe(o,{childList:true,subtree:true});}")
    await b.js("(()=>{const ev=" + WRAP + "(8,'A LIVE HELLO',0);"
               "for(const r of __reqs)if(r.fs.some(f=>(f.kinds||[]).includes(1059)&&f.limit==null&&!f['#k']))"
               "r.sock.fire('message',['EVENT',r.sub,ev]);})()")
    await b.until("[...document.querySelectorAll('#dm-badge,#dm-badge-m')].some(e=>!e.classList.contains('hidden')&&+e.textContent>=1)")
    await b.until("__toasts.some(t=>/sent you a message/.test(t))")

    await b.js("__PC.switchView('messages')")
    await b.until(f"!!document.querySelector('#dm-rows .dm-peer[data-peer=\"{bob}\"]')")
    await b.js(f"document.querySelector('#dm-rows .dm-peer[data-peer=\"{bob}\"]').click()")
    await b.until("(document.querySelector('#dm-thread')||{}).innerText && "
                  "document.querySelector('#dm-thread').innerText.includes('A LIVE HELLO')")

    # The DmCache proxy reaches the module's own object (what logout's DmCache.forget goes through),
    # and the counters dms.js writes through its setters are the ones app.js reads.
    stats = await b.js("__PC.dmStats()")
    assert stats['done'] >= 2 and stats['total'] >= 2, stats

    fetched = await b.js("performance.getEntriesByType('resource').filter(e=>/\\/dms\\.js(\\?|$)/.test(e.name)).length")
    assert fetched == 1, fetched
    assert await b.js("window.__dmBuilds") == 1
    assert not await b.js('__errors'), await b.js('__errors')
    assert not await b.js('__consoleErrors'), await b.js('__consoleErrors')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_bundled_client_boots_with_dms_js_and_a_dm_arrives_and_opens():
    asyncio.run(desktop.with_browser('online', '', _boot_receive_and_open, extra_init=RELAY + BUILD_PROBE))
