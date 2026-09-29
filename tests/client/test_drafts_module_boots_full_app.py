"""The real bundled client boots with drafts.js split out of app.js: a draft survives a reload and
opens in the composer, and a scheduled post is listed.

Drives the SHIPPED desktop bundle (desktop/build-www.sh renders templates/client.html and copies
every client script) in headless Chrome, through the same harness and in-page relay as the DM module
test. Only the relay socket and two instance HTTP endpoints are fixtures (`/client/drafts`, which
echoes what it is sent the way the server's copy does, and `/client/scheduled/list`, answering one
pending post); everything that saves, counts, lists, reopens and schedules is the shipped code.
What a broken split looks like:

  * drafts.js loaded after app.js (or missing) -> the factory is built later, by whichever call
    reaches a forwarder or the Drafts/Scheduled proxy first — caught by WHERE it was built: once,
    during app.js's own evaluation, from `_draftsMod`, never from `_lzRun`;
  * the Drafts proxy not reaching the module's object -> the composer's 💾 (compose.js calls
    `Drafts.save` through it) saves nothing, or the badge never counts;
  * a dependency not passed (compose, linkify, timeAgo, selfProof, …) or a live binding captured by
    value (`S.ME`, `S.VIEW`) -> the drafts are written under the wrong account, the Drafts view
    never lists, the ⏰ section never fills, or a ReferenceError;
  * and no page error or console error anywhere.
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
{let real;Object.defineProperty(window,'PCDraftsFactory',{configurable:true,get(){return real;},set(f){
  real=function(dep){
    window.__draftBuilds=(window.__draftBuilds||0)+1;
    window.__draftBuiltIn=(document.currentScript&&document.currentScript.src||'').split('?')[0].split('/').pop();
    window.__draftBuiltStack=String(new Error().stack);
    window.__draftBuiltBeforePC=!window.__PC;
    return f.apply(this,arguments);
  };}});}
// The instance's two drafts endpoints. /client/drafts keeps what it is sent (as the server's copy of
// the document does) so a reload's pull() merges it back; /client/scheduled/list answers one post.
window.__schedAsks=0;
{const f0=window.fetch;window.fetch=function(url,opts){const u=String(url);
 const json=d=>Promise.resolve(new Response(JSON.stringify(d),{status:200,headers:{'Content-Type':'application/json'}}));
 if(u.includes('/client/scheduled/list')){__schedAsks++;return json({ok:true,posts:[{id:77,status:'pending',
   scheduled_at:Math.floor(Date.now()/1000)+86400,preview:'A POST FOR TOMORROW'}]});}
 if(u.includes('/client/drafts')){const b=JSON.parse((opts&&opts.body)||'{}');
   if(Array.isArray(b.drafts))localStorage.setItem('__remoteDrafts',JSON.stringify(b.drafts));
   return json({ok:true,drafts:JSON.parse(localStorage.getItem('__remoteDrafts')||'[]')});}
 return f0.apply(this,arguments);};}
'''

TEXT = 'A DRAFT THAT OUTLIVES A RELOAD'


async def _built_once_during_app_js(b):
    order = await b.js("[...document.scripts].map(s=>(s.getAttribute('src')||'').split('?')[0].split('/').pop())"
                       ".filter(n=>n==='drafts.js'||n==='app.js')")
    assert order == ['drafts.js', 'app.js'], order
    assert await b.js("window.__draftBuilds") == 1
    assert await b.js("window.__draftBuiltIn") == 'app.js'
    assert await b.js("window.__draftBuiltBeforePC") is True
    stack = await b.js("window.__draftBuiltStack")
    assert '_draftsMod' in stack and '_lzRun' not in stack, stack


async def _save_reload_reopen(b):
    await _built_once_during_app_js(b)
    await desktop.login(b)
    me = await b.js("__PC.me().pubkey")

    # 1. SAVE: the real composer's 💾 goes through compose.js -> the Drafts proxy -> drafts.js.
    await b.js("__PC.compose()")
    await b.until("!!document.querySelector('#cmp') && !!document.querySelector('#cmp-draft')")
    await b.js("(()=>{const ta=document.querySelector('#cmp');ta.value=" + json.dumps(TEXT) + ";"
               "ta.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#cmp-draft').click();})()")
    await b.until(f"JSON.parse(localStorage.getItem('pc_drafts_{me}')||'[]').some(d=>!d.del&&d.text==={json.dumps(TEXT)})")
    await b.until("[...document.querySelectorAll('#draft-badge')].some(e=>!e.classList.contains('hidden')&&e.textContent==='1')")

    # 2. RELOAD: a fresh page builds drafts.js again, during app.js, and the draft is still there.
    await b.call('Page.reload', {'ignoreCache': True})
    await b.until('!!window.__PC && !!__PC.me()')
    await _built_once_during_app_js(b)
    await b.js("__PC.switchView('drafts')")
    card = ".draft-card[data-draft]"
    await b.until(f"[...document.querySelectorAll({json.dumps(card)})].some(c=>c.innerText.includes({json.dumps(TEXT)}))")
    # The ⏰ Scheduled section above the drafts, filled from Scheduled.list() through its proxy.
    await b.until("!!document.querySelector('#sched-section .sched-card[data-sid=\"77\"]') && "
                  "document.querySelector('#sched-section').innerText.includes('A POST FOR TOMORROW')")
    assert await b.js("__schedAsks") >= 1

    # 3. OPEN: Edit puts the draft back into the composer.
    await b.js(f"[...document.querySelectorAll({json.dumps(card)})].find(c=>c.innerText.includes({json.dumps(TEXT)}))"
               ".querySelector('[data-act=edit]').click()")
    await b.until(f"(document.querySelector('#cmp')||{{}}).value==={json.dumps(TEXT)}")

    fetched = await b.js("performance.getEntriesByType('resource').filter(e=>/\\/drafts\\.js(\\?|$)/.test(e.name)).length")
    assert fetched == 1, fetched
    assert await b.js("window.__draftBuilds") == 1
    assert not await b.js('__errors'), await b.js('__errors')
    assert not await b.js('__consoleErrors'), await b.js('__consoleErrors')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_the_bundled_client_boots_with_drafts_js_and_a_draft_survives_a_reload():
    asyncio.run(desktop.with_browser('online', '', _save_reload_reopen, extra_init=RELAY + BUILD_PROBE))
