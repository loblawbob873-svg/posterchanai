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


GATEWAY_PAGE = r'''
{const f0=window.fetch;window.fetch=function(url,opts){const u=String(url);
 if(u.includes('/client/scheduled')&&!u.includes('/list')&&!u.includes('/cancel')){window.__schedTries=(window.__schedTries||0)+1;
   return Promise.resolve(new Response('<html><head><title>502 Bad Gateway</title></head><body><center><h1>502 Bad Gateway</h1></center><hr><center>nginx</center></body></html>',
     {status:502,headers:{'Content-Type':'text/html'}}));}
 if(u.includes('/client/scheduled/list'))return Promise.resolve(new Response(JSON.stringify({ok:true,posts:[]}),{status:200,headers:{'Content-Type':'application/json'}}));
 if(u.includes('/client/drafts'))return Promise.resolve(new Response(JSON.stringify({ok:true,drafts:[]}),{status:200,headers:{'Content-Type':'application/json'}}));
 return f0.apply(this,arguments);};}
'''


async def _schedule_into_a_gateway_page(b):
    await desktop.login(b)
    await _open_composer(b, TEXT)
    await b.js("document.querySelector('#cmp-sched-row .sched-chip[data-min=\"60\"]').click()")
    await b.until("/publishes/.test(document.querySelector('#cmp-sched-when').textContent)")
    await b.js("document.querySelector('#cmp-sched-go').click()")
    await b.until("(window.__schedTries||0)>=1 && /schedule failed/.test(document.querySelector('#cmp-status').textContent)")
    said = await b.js("document.querySelector('#cmp-status').textContent")
    assert 'Unexpected token' not in said and 'JSON' not in said, ("a gateway page read as a JSON error", said)
    assert '502' in said and 'NOT scheduled' in said, said
    assert await b.js("!!document.querySelector('#cmp') && document.querySelector('#cmp').value") == TEXT, \
        'the composer closed (or lost the text) on a schedule that never happened'
    assert not await b.js("document.querySelector('#cmp-sched-go').disabled"), 'Schedule stayed disabled: no way to retry'


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_error_page_from_the_instance_says_not_scheduled_not_unexpected_token():
    """"scheduled posts is giving invalid json, scheduled failed, unexpected token" (2026-10-16): the instance
    answered with nginx's HTML 502 (the app restarting for a deploy) and the client parsed it as JSON."""
    asyncio.run(desktop.with_browser('online', '', _schedule_into_a_gateway_page, extra_init=RELAY + GATEWAY_PAGE))


UNREADABLE = r'''
window.__listFail=true;window.__cancelFail=false;window.__listCalls=0;window.__cancels=[];
window.__posts=[{id:700,status:'pending',scheduled_at:Math.floor(Date.now()/1000)+7200,preview:'STILL GOING OUT AT NINE'}];
{const f0=window.fetch;window.fetch=function(url,opts){const u=String(url);
 const json=(d,s)=>Promise.resolve(new Response(JSON.stringify(d),{status:s||200,headers:{'Content-Type':'application/json'}}));
 const down=()=>json({ok:false,error:'scheduled posts are unavailable right now — this node’s datastore could not be asked; try again shortly'},503);
 if(u.includes('/client/scheduled/list')){__listCalls++;return __listFail?down():json({ok:true,posts:__posts});}
 if(u.includes('/client/scheduled/cancel')){__cancels.push(JSON.parse(opts.body).id);return __cancelFail?down():json({ok:true});}
 if(u.includes('/client/drafts'))return json({ok:true,drafts:[]});
 return f0.apply(this,arguments);};}
'''


async def _unreadable_schedule_is_said(b):
    await desktop.login(b)
    await b.js("window.__toasts=[];new MutationObserver(()=>{document.querySelectorAll('.toast').forEach(t=>__toasts.push(t.textContent))})"
               ".observe(document.body,{childList:true,subtree:true});")
    # A fresh device opens Drafts while the node cannot read the schedule: it SAYS so -- never an empty section.
    await b.js("__PC.switchView('drafts')")
    await b.until("__listCalls>=1 && !!document.querySelector('#sched-section #sched-unreadable')")
    said = await b.js("document.querySelector('#sched-unreadable').innerText")
    assert 'Couldn’t load your scheduled posts' in said and 'still scheduled' in said, said
    assert 'unavailable right now' in said, ('the reason is not said', said)
    assert not await b.js("!!document.querySelector('#sched-section .sched-card')")
    # The node answers again: Try again shows what is scheduled, and the notice goes.
    await b.js("__listFail=false; document.querySelector('#sched-retry').click()")
    card = '#sched-section .sched-card[data-sid="700"]'
    await b.until(f"!!document.querySelector({json.dumps(card)}) && !document.querySelector('#sched-unreadable')")
    # A cancel the node could not take: said as such, and the post is still listed as scheduled.
    await b.js("__cancelFail=true")
    await b.js(f"document.querySelector({json.dumps(card)} + ' [data-act=cancel]').click()")
    await _confirm(b)
    await b.until("__cancels.length===1 && __toasts.some(t=>/STILL SCHEDULED/.test(t))")
    toasts = await b.js("__toasts")
    assert not [t for t in toasts if 'too late' in t], ('a cancel that never reached the node was called "already posted"', toasts)
    await b.until(f"!!document.querySelector({json.dumps(card)}) && !document.querySelector({json.dumps(card)} + ' [data-act=cancel]').disabled")
    # ...and once it can, the same button cancels it.
    await b.js("__cancelFail=false;__posts=[]")
    await b.js(f"document.querySelector({json.dumps(card)} + ' [data-act=cancel]').click()")
    await _confirm(b)
    await b.until(f"__cancels.length===2 && !document.querySelector({json.dumps(card)})")
    assert not await b.js('__errors'), await b.js('__errors')


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_schedule_the_node_cannot_read_is_said_and_a_failed_cancel_is_not_called_too_late():
    """Drafts showed NOTHING when /client/scheduled/list failed -- the look of "nothing scheduled" -- and every
    failed cancel was toasted "too late — it already posted" about a post that was still going out."""
    asyncio.run(desktop.with_browser('online', '', _unreadable_schedule_is_said, extra_init=RELAY + UNREADABLE))
