"""Scheduling a post, end to end in the real bundled client ("make sure schedule posts have test cases").

The server half is covered by tests/test_scheduled_posts_endpoints.py (what /client/scheduled accepts)
and tests/test_a_scheduled_post_goes_out_once.py (the publisher). This is the CLIENT half, which had
nothing: the composer's ⏰ Schedule → a chip → Schedule, the Drafts screen's ⏰ section, and its
Cancel / Dismiss. Only the relay socket and the three /client/scheduled* endpoints are fixtures (a
tiny in-page server that keeps what it is sent, the way the real table does); the composer, signer,
Scheduled store and Drafts view are the shipped code.

What it pins, each a way the feature could break with nothing on screen:
  * the queued event is SIGNED by the user (valid sig, their pubkey), its created_at IS the schedule
    (the server refuses a mismatch), it carries the text, and it was NOT also published right now;
  * the composer refuses a time in the past instead of queuing it;
  * the post appears under ⏰ Scheduled in Drafts, and Cancel takes it out of the queue;
  * a cancel that loses to the publisher says "too late — it already posted";
  * a FAILED schedule is shown as failed with Dismiss, and one mid-send offers no button.
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


SERVER = r'''
window.__schedPosts=[];window.__schedCreates=[];window.__schedCancels=[];window.__cancelLoses=false;
{const f0=window.fetch;window.fetch=function(url,opts){const u=String(url);
 const json=d=>Promise.resolve(new Response(JSON.stringify(d),{status:200,headers:{'Content-Type':'application/json'}}));
 const body=()=>JSON.parse((opts&&opts.body)||'{}');
 if(u.includes('/client/scheduled/list'))return json({ok:true,posts:__schedPosts.filter(p=>p.status!=='cancelled'&&p.status!=='sent')});
 if(u.includes('/client/scheduled/cancel')){const b=body();__schedCancels.push(b.id);
   if(__cancelLoses){const p=__schedPosts.find(p=>String(p.id)===String(b.id));if(p)p.status='sent';return json({ok:false});}
   const p=__schedPosts.find(p=>String(p.id)===String(b.id)&&(p.status==='pending'||p.status==='failed'));
   if(p)p.status='cancelled';return json({ok:!!p});}
 if(u.includes('/client/scheduled')){const b=body();__schedCreates.push(b);
   const id=100+__schedPosts.length;
   __schedPosts.push({id,status:'pending',scheduled_at:b.scheduled_at,preview:(b.event&&b.event.content)||'',event_id:b.event&&b.event.id});
   return json({ok:true,id,scheduled_at:b.scheduled_at});}
 if(u.includes('/client/drafts'))return json({ok:true,drafts:[]});
 return f0.apply(this,arguments);};}
'''

TEXT = 'A POST THAT GOES OUT IN AN HOUR'


async def _open_composer(b, text):
    await b.js("__PC.compose()")
    await b.until("!!document.querySelector('#cmp') && !!document.querySelector('#cmp-sched-btn')")
    await b.js("(()=>{const ta=document.querySelector('#cmp');ta.value=" + json.dumps(text) + ";"
               "ta.dispatchEvent(new Event('input',{bubbles:true}));"
               "document.querySelector('#cmp-sched-btn').click();})()")
    await b.until("!document.querySelector('#cmp-sched-row').classList.contains('hidden')")


async def _confirm(b):
    await b.until("!!document.querySelector('.uiconfirm-bg [data-uc=\"1\"]')")
    await b.js("document.querySelector('.uiconfirm-bg [data-uc=\"1\"]').click()")


async def _schedule_list_cancel(b):
    await desktop.login(b)
    me = await b.js("__PC.me().pubkey")

    # 1. A time in the past is refused in the composer, and nothing is queued.
    await _open_composer(b, TEXT)
    await b.js("(()=>{const d=new Date(Date.now()-3600e3),p=n=>String(n).padStart(2,'0');"
               "document.querySelector('#cmp-sched-at').value=`${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;"
               "document.querySelector('#cmp-sched-go').click();})()")
    await b.until("/at least a minute from now/.test(document.querySelector('#cmp-status').textContent)")
    assert await b.js("__schedCreates.length") == 0

    # 2. +1h → Schedule: a signed note, created_at == the schedule, queued and NOT published now.
    published_before = await b.js("__published.filter(e=>e.kind===1).length")
    await b.js("document.querySelector('#cmp-sched-row .sched-chip[data-min=\"60\"]').click()")
    # The field has minute precision, so +1h reads "in 1 h" or, seconds later, "in 59 min".
    await b.until("/publishes .* \\(in (1 h|59 min)\\)/.test(document.querySelector('#cmp-sched-when').textContent)")
    t0 = await b.js("Math.floor(Date.now()/1000)")
    await b.js("document.querySelector('#cmp-sched-go').click()")
    await b.until("__schedCreates.length===1")
    sent = await b.js("__schedCreates[0]")
    ev = sent['event']
    assert ev['kind'] == 1 and ev['content'] == TEXT and ev['pubkey'] == me
    assert sent['pubkey'] == me and sent.get('auth'), 'the request carries an ownership proof'
    assert ev['created_at'] == sent['scheduled_at'], 'the server refuses an event whose time is not the schedule'
    assert abs(sent['scheduled_at'] - (t0 + 3600)) <= 90, (sent['scheduled_at'], t0)
    assert await b.js(f"NostrTools.verifyEvent({json.dumps(ev)})") is True, 'the queued note is not validly signed'
    await b.until("!document.querySelector('#cmp')")            # the composer closed on success
    assert await b.js("__published.filter(e=>e.kind===1).length") == published_before, \
        'a scheduled note was ALSO published immediately'

    # 3. Drafts lists it under ⏰ Scheduled; Cancel removes it from the queue and the screen.
    await b.js("__PC.switchView('drafts')")
    card = '#sched-section .sched-card[data-sid="100"]'
    await b.until(f"!!document.querySelector({json.dumps(card)}) && "
                  f"document.querySelector({json.dumps(card)}).innerText.includes({json.dumps(TEXT)})")
    assert await b.js("document.querySelector('#sched-section .sched-head').textContent.includes('Scheduled · 1')")
    await b.js(f"document.querySelector({json.dumps(card)} + ' [data-act=cancel]').click()")
    await _confirm(b)
    await b.until("__schedCancels.length===1")
    assert await b.js("String(__schedCancels[0])") == '100'
    await b.until(f"!document.querySelector({json.dumps(card)})")

    # 4. A cancel that loses to the publisher says so.
    await asyncio.sleep(4.2)       # Scheduled.list() keeps its answer 4s, so quick re-renders do not refetch
    await b.js("__schedPosts.push({id:200,status:'pending',scheduled_at:Math.floor(Date.now()/1000)+600,preview:'ABOUT TO GO'});"
               "__cancelLoses=true;__PC.switchView('notifications');")
    await b.js("__PC.switchView('drafts')")
    late = '#sched-section .sched-card[data-sid="200"]'
    await b.until(f"!!document.querySelector({json.dumps(late)})")
    await b.js("window.__toasts=[];const _o=document.body;new MutationObserver(()=>{document.querySelectorAll('.toast').forEach(t=>__toasts.push(t.textContent))}).observe(_o,{childList:true,subtree:true});")
    await b.js(f"document.querySelector({json.dumps(late)} + ' [data-act=cancel]').click()")
    await _confirm(b)
    await b.until("__toasts.some(t=>/too late/.test(t)) || /too late/.test(document.body.innerText)")
    await b.js("__cancelLoses=false")

    # 5. Failed → shown as failed with Dismiss; sending → no button at all.
    await asyncio.sleep(4.2)
    await b.js("__schedPosts.push({id:300,status:'failed',scheduled_at:Math.floor(Date.now()/1000)-600,preview:'NEVER WENT'},"
               "{id:301,status:'sending',scheduled_at:Math.floor(Date.now()/1000)-5,preview:'GOING NOW'});"
               "__PC.switchView('notifications');")
    await b.js("__PC.switchView('drafts')")
    await b.until("!!document.querySelector('#sched-section .sched-card[data-sid=\"300\"]') && "
                  "!!document.querySelector('#sched-section .sched-card[data-sid=\"301\"]')")
    failed = await b.js("(()=>{const c=document.querySelector('#sched-section .sched-card[data-sid=\"300\"]');"
                        "return {cls:c.classList.contains('sched-failed'),txt:c.innerText,btn:(c.querySelector('[data-act=cancel]')||{}).textContent||''}})()")
    assert failed['cls'] and 'failed to publish' in failed['txt'] and 'Dismiss' in failed['btn'], failed
    assert await b.js("!document.querySelector('#sched-section .sched-card[data-sid=\"301\"] [data-act=cancel]')"), \
        'a post being sent right now must not offer Cancel'
    await b.js("document.querySelector('#sched-section .sched-card[data-sid=\"300\"] [data-act=cancel]').click()")
    await _confirm(b)
    await b.until("!document.querySelector('#sched-section .sched-card[data-sid=\"300\"]')")

    assert not await b.js('__errors'), await b.js('__errors')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_post_is_scheduled_listed_and_cancelled_in_the_real_client():
    asyncio.run(desktop.with_browser('online', '', _schedule_list_cancel, extra_init=RELAY + SERVER))
