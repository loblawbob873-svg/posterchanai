"""Texts ✨ — a suggested reply, driven in the REAL bundled Texts renderer at phone and desktop width.

What the person sees is the contract: a ✨ button in the composer row (lined up with the others), a
tap that sends the bounded tail of the conversation to the node, a busy state a second tap cannot
get past, and the reply landing IN THE COMPOSER — unsent. Without AI access (the server's probe says
no, or a Nostr-only/no-instance build) there is no button at all. A failure is a sentence.

Only the network boundary is a fixture: `__PC.authFetch` answers /api/texts/ai-reply.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_sms_image_paste_full_app import open_texts


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# 14 messages, alternating, newest last — an incoming question the ✨ should answer.
SEED = r"""
(()=>{
  const S=PCSms._state(), now=Date.now();
  for(let i=0;i<14;i++){
    const last=i===13;
    S.msgs.set('ai-doc-'+i,{address:'+15550100',body:last?'Dinner at 7 tonight?':'earlier message '+i,
      date:now-(14-i)*60000,incoming:last?true:(i%2===0),doc:'ai-doc-'+i,parts:[]});
  }
})()
"""

NET = r"""
window.aiCalls=[];window.aiAllowed=true;window.aiHold=true;
__PC.ensureAiSession=async()=>({can_ai:true});
__PC.authFetch=async(url,opts={})=>{
  const body=JSON.parse(opts.body||'{}');
  if(!String(url).includes('/api/texts/ai-reply'))return new Response('{}',{status:404});
  if(body.probe)return new Response(JSON.stringify({ok:true,allowed:aiAllowed}),{status:200});
  aiCalls.push(body);
  if(!aiHold)return new Response(JSON.stringify({ok:false,error:'The AI did not answer — try again in a moment.'}),{status:502});
  return await new Promise(resolve=>{window.finishAi=(content)=>resolve(new Response(JSON.stringify({ok:true,content}),{status:200}))});
};
"""

MEASURE = r"""(()=>{
  const b=document.querySelector('#sms-ai');
  const box=e=>{const r=e.getBoundingClientRect();return {id:e.id,l:r.left,t:r.top,r:r.right,b:r.bottom,w:r.width,h:r.height}};
  const row=[...document.querySelectorAll('.sms-compose > *')].filter(e=>e.getBoundingClientRect().width>0).map(box);
  return {exists:!!b,inCompose:!!(b&&b.closest('.sms-compose')),visible:!!(b&&b.getBoundingClientRect().width>0),
          busy:!!(b&&b.disabled&&b.getAttribute('aria-busy')==='true'),row,vw:innerWidth,
          input:(document.querySelector('#sms-in')||{}).value};
})()"""


async def _ready(b):
    await open_texts(b)                                  # the real renderer, a conversation open
    await b.until("!PCSms._ai().probing")                # the boot probe (fixture network) settled
    await b.js(NET)
    await b.js(SEED)
    await b.js("PCSms._aiReset();PCSms.refreshNames()")
    await b.until("!!document.querySelector('#sms-ai') && document.querySelector('#sms-ai').getBoundingClientRect().width>0")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('phone', [True, False])
def test_sparkle_fills_the_composer_with_a_draft_and_never_sends(phone):
    async def check(b):
        w, h = (390, 844) if phone else (1280, 900)
        await b.call('Emulation.setDeviceMetricsOverride', {'width': w, 'height': h, 'deviceScaleFactor': 1, 'mobile': phone})
        if phone:
            await b.call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
        await _ready(b)
        m = await b.js(MEASURE)
        assert m['exists'] and m['inCompose'] and m['visible'], m
        # It lines up with the rest of the composer row and fits on a phone.
        assert len({round(r['h']) for r in m['row']}) == 1, m['row']
        assert len({round(r['t']) for r in m['row']}) == 1, m['row']
        for r in m['row']:
            assert r['l'] >= 0 and r['r'] <= m['vw'] + 0.5, r
        ai = next(r for r in m['row'] if r['id'] == 'sms-ai')
        if phone:
            assert ai['h'] >= 40 and ai['w'] >= 36, ai
        inp = next(r for r in m['row'] if r['id'] == 'sms-in')
        assert inp['w'] >= 90, inp                                    # the text box is still usable

        # Tap: one request, the bounded tail with who said what, the last message last.
        await b.js("document.querySelector('#sms-ai').click()")
        await b.until("aiCalls.length===1")
        req = (await b.js("aiCalls[0]"))
        msgs = req['messages']
        assert len(msgs) == 10, msgs
        assert msgs[-1] == {'me': False, 'text': 'Dinner at 7 tonight?'}, msgs[-1]
        assert msgs[0]['text'] == 'earlier message 4', msgs[0]
        # No number, no name: the thread tail and how many drafts to offer (the ✨ menu shows 3).
        assert set(req) == {'messages', 'count'} and req['count'] == 3, req
        assert all(set(x) == {'me', 'text'} for x in msgs)

        # Busy: disabled + aria-busy, a second tap and a repaint do not start another request.
        assert (await b.js(MEASURE))['busy']
        await b.js("document.querySelector('#sms-ai').click();PCSms.refreshNames();document.querySelector('#sms-ai').click()")
        await asyncio.sleep(0.3)
        assert await b.js("aiCalls.length") == 1
        assert (await b.js(MEASURE))['busy'], 'a repaint lost the busy state'

        await b.js("finishAi('Yes! See you at 7')")
        await b.until("!document.querySelector('#sms-ai').disabled")
        got = await b.js(MEASURE)
        assert got['input'] == 'Yes! See you at 7', got
        assert not got['busy']
        # In the composer, never sent: nothing was published, and the draft is the conversation's.
        assert await b.js("sends.length") == 0
        assert await b.js("PCSms._state().draft[PCSms._state().open].text") == 'Yes! See you at 7'

    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_sparkle_is_hidden_without_ai_access_and_failures_say_so():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 390, 'height': 844, 'deviceScaleFactor': 1, 'mobile': True})
        await _ready(b)

        # A failure is a sentence, and what the person typed is left alone.
        await b.js("aiHold=false;const i=document.querySelector('#sms-in');i.value='my own words';i.dispatchEvent(new Event('input'));document.querySelector('#sms-ai').click()")
        await b.until("toasts.some(t=>/did not answer/.test(t))")
        assert await b.js("document.querySelector('#sms-in').value") == 'my own words'
        assert await b.js("sends.length") == 0

        # The server says this account has no AI: no button.
        await b.js("aiAllowed=false;PCSms._aiReset();PCSms.refreshNames()")
        await b.until("PCSms._ai().reply===false")
        await b.js("PCSms.refreshNames()")
        assert await b.js("(()=>{const b=document.querySelector('#sms-ai');return !b||b.getBoundingClientRect().width===0})()")

        # A Nostr-only node: no button, and it never even asks.
        await b.js("aiAllowed=true;window.PC_NOSTR_ONLY=true;PCSms._aiReset();window.probes=0;const f=__PC.authFetch;__PC.authFetch=(u,o)=>{probes++;return f(u,o)};PCSms.refreshNames()")
        await asyncio.sleep(0.3)
        assert await b.js("(()=>{const b=document.querySelector('#sms-ai');return !b||b.getBoundingClientRect().width===0})()")
        assert await b.js("probes") == 0
    extra = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
    asyncio.run(desktop.with_browser('online', '', check, extra))


def test_the_context_builder_is_bounded_and_carries_no_identity():
    """Source-level floor for the builder the browser test exercises: the window is 10 and a
    message is reduced to {me, text} — nothing else from the row can reach the request."""
    src = (Path(__file__).resolve().parents[2] / 'static/js/client/sms.js').read_text()
    body = src[src.index('function aiContext('):src.index('async function aiSuggest(')]
    assert "out.push({ me: !m.incoming, text: text.slice(0, 1000) })" in body
    assert 'const AI_CONTEXT = 10;' in src
    assert 'return out.slice(-AI_CONTEXT);' in body
