"""The real bundled client boots with notifs.js split out of app.js, and a reply to you raises the
badge, lists in Notifications and opens the post.

Drives the SHIPPED desktop bundle (desktop/build-www.sh renders templates/client.html and copies
every client script) in headless Chrome, through the same harness and in-page relay as the DM and
drafts module tests. Only the relay socket is a fixture; the events are REAL signed Nostr events (a
note of yours on the relay, a mention already there, and a reply pushed live), and everything that
subscribes, counts, pings, lists and opens them is the shipped code. What a broken split looks like:

  * notifs.js loaded after app.js (or missing) -> the factory is built later, by whichever call
    reaches a forwarder first (or fetched again by the lazy loader) — and the block's top-level
    statements (the follower pins, the epoch repair) run late — caught by WHERE it was built: once,
    during app.js's own evaluation, from `_notifsMod`, never from `_lzRun`;
  * a dependency not passed (seenNotif, notifToast's LOGO, osNotify, _notifCtxId, …) or a live
    binding captured by value (`S.ME`, `S.VIEW`, `S._notifEpoch`, `S._newBuild`) -> no subscription,
    no badge, no toast, or a ReferenceError the first time an event arrives;
  * and no page error or console error anywhere.

Two deliveries, because they are two code paths: the BACKLOG answered before the subscription's
EOSE (counted by bumpNotif on EOSE, never pinged) and a LIVE reply after it (bumpNotif + notifPing
→ the in-app notification toast).
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
{let real;Object.defineProperty(window,'PCNotifsFactory',{configurable:true,get(){return real;},set(f){
  real=function(dep){
    window.__notifBuilds=(window.__notifBuilds||0)+1;
    window.__notifBuiltIn=(document.currentScript&&document.currentScript.src||'').split('?')[0].split('/').pop();
    window.__notifBuiltStack=String(new Error().stack);
    window.__notifBuiltBeforePC=!window.__PC;
    // The block's top-level statements ran inside the build: the epoch app.js set just above it is
    // what the one-time repair stamped as done.
    window.__notifEpochAtBuild=localStorage.getItem('pc_notif_epoch');
    const m=f.apply(this,arguments);
    window.__notifMigratedAtBuild=localStorage.getItem('pc_notif_epoch_migrated');
    return m;
  };}});}
// Every REQ the client writes, so the test can push a LIVE event down the subscription it opened.
window.__reqs=[];
{const send=RelaySocket.prototype.send;RelaySocket.prototype.send=function(raw){
  try{const m=JSON.parse(raw);if(Array.isArray(m)&&m[0]==='REQ')__reqs.push({sock:this,sub:m[1],fs:m.slice(2)});}catch(_){}
  return send.apply(this,arguments);};}
'''

# A kind-1 signed by the 32-byte fill `sk`, `ago` seconds old, with the given tags.
NOTE = r'''((sk,text,ago,tags)=>NostrTools.finalizeEvent({kind:1,created_at:Math.floor(Date.now()/1000)-ago,
  tags:tags||[],content:text},new Uint8Array(32).fill(sk)))'''
ME_PK = "NostrTools.getPublicKey(new Uint8Array(32).fill(1))"
MENTIONS = "__reqs.filter(r=>r.fs.some(f=>(f.kinds||[]).includes(9735)&&(f['#p']||[]).length))"


async def _boot_notify_and_open(b):
    order = await b.js("[...document.scripts].map(s=>(s.getAttribute('src')||'').split('?')[0].split('/').pop())"
                       ".filter(n=>n==='notifs.js'||n==='app.js')")
    assert order == ['notifs.js', 'app.js'], order
    assert await b.js("window.__notifBuilds") == 1
    assert await b.js("window.__notifBuiltIn") == 'app.js'
    assert await b.js("window.__notifBuiltBeforePC") is True
    stack = await b.js("window.__notifBuiltStack")
    assert '_notifsMod' in stack and '_lzRun' not in stack, stack
    assert await b.js("!!window.__notifEpochAtBuild && window.__notifMigratedAtBuild==='1'")

    # The relay already holds a note of mine and somebody's mention of me (the backlog).
    await b.js("(()=>{const me=" + ME_PK + ";const r=_rel();"
               "const mine=" + NOTE + "(1,'MY OWN POST',900);r.push(mine);"
               "r.push(" + NOTE + "(9,'A MENTION FROM THE BACKLOG',600,[['p',me]]));"
               "localStorage.setItem('__relayEvents',JSON.stringify(r));window.__mine=mine;})()")
    await desktop.login(b)
    await b.until(MENTIONS + ".length>0")
    # 1. BACKLOG: counted by bumpNotif at the subscription's EOSE -> the unread badge.
    await b.until("[...document.querySelectorAll('#notif-badge,#notif-badge-m')].some(e=>!e.classList.contains('hidden')&&+e.textContent>=1)")
    assert await b.js("__PC.notifUnread()") >= 1

    # 2. LIVE: a reply to my post, pushed down the mentions subscription after its EOSE -> the badge
    #    rises and notifPing raises the in-app toast.
    await b.js("window.__toasts=[];new MutationObserver(()=>{document.querySelectorAll('.notif-toast').forEach(t=>"
               "__toasts.includes(t.innerText)||__toasts.push(t.innerText));}).observe(document.body,{childList:true,subtree:true});")
    before = await b.js("__PC.notifUnread()")
    await b.js("(()=>{const me=" + ME_PK + ";const reply=" + NOTE + "(7,'A LIVE REPLY TO YOU',0,"
               "[['e',__mine.id,'','root'],['p',me]]);window.__reply=reply;"
               "const r=_rel();r.push(reply);localStorage.setItem('__relayEvents',JSON.stringify(r));"
               "for(const q of " + MENTIONS + ")q.sock.fire('message',['EVENT',q.sub,reply]);})()")
    await b.until(f"__PC.notifUnread()>{before}")
    await b.until("__toasts.some(t=>/replied to you/.test(t))")

    # 3. The Notifications view lists it (notifList is the gate), and opening it routes to the post.
    await b.js("__PC.switchView('notifications')")
    row = ".notif[data-open]"
    await b.until(f"[...document.querySelectorAll({json.dumps(row)})].some(n=>n.innerText.includes('A LIVE REPLY TO YOU'))")
    target = await b.js(f"[...document.querySelectorAll({json.dumps(row)})].find(n=>n.innerText.includes('A LIVE REPLY TO YOU')).dataset.open")
    assert target in (await b.js("__mine.id"), await b.js("__reply.id")), target
    await b.js(f"[...document.querySelectorAll({json.dumps(row)})].find(n=>n.innerText.includes('A LIVE REPLY TO YOU')).click()")
    await b.until("(()=>{try{const d=NostrTools.nip19.decode(location.pathname.slice(1));"
                  "return (d.data.id||d.data)===" + json.dumps(target) + ";}catch(_){return false;}})()")
    await b.until("document.querySelector('#feed').innerText.includes('MY OWN POST') && "
                  "document.querySelector('#feed').innerText.includes('A LIVE REPLY TO YOU')")

    fetched = await b.js("performance.getEntriesByType('resource').filter(e=>/\\/notifs\\.js(\\?|$)/.test(e.name)).length")
    assert fetched == 1, fetched
    assert await b.js("window.__notifBuilds") == 1
    assert not await b.js('__errors'), await b.js('__errors')
    assert not await b.js('__consoleErrors'), await b.js('__consoleErrors')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_bundled_client_boots_with_notifs_js_and_a_reply_notifies_and_opens():
    asyncio.run(desktop.with_browser('online', '', _boot_notify_and_open, extra_init=RELAY + BUILD_PROBE))
