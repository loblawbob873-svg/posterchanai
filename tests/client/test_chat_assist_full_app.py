"""✨ in Telegram and Direct Messages: Generate reply, Summarize chat, Summarize YouTube & links.

Asked for: "AI replies to Telegram and DM's also, make sure mobile UI looks good" and "For Telegram,
maybe the AI sparkle button should have a Generate Reply Summarize youtube and links". Driven in the
real bundled client (Telegram at desktop and tablet width, DMs at phone and desktop width); only
/api/chat-assist is a fixture. Pinned, on both screens:

  * the ✨ sits in the composer row, on screen, and is absent when the server says no AI;
  * the menu offers the links item only when the chat has a link;
  * a reply is drafted from THIS chat (who said what, oldest first), several drafts come back as a
    menu, the pick lands in the composer, and nothing is ever sent;
  * a summary and the link summaries are READ in a sheet that fits the screen, with Copy;
  * dismissing a menu leaves the ✨ usable (a dismissed menu used to be able to hold it busy).
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_telegram_client_full_app import FAKE, _open as _open_tg
from tests.client.test_dm_newest_first_full_app import RELAY
from tests.client.test_dm_send_scrolls_to_bottom_full_app import SEED as DM_SEED


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLAIN = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
CHOICES = ["Sounds good!", "Can we do Friday instead? I have a thing on Thursday evening that runs late", "Who else is coming?"]

ASSIST = r"""
window.__assist={calls:[],allowed:true};
const __fa=window.fetch;
window.fetch=function(url,opts){
  const u=String(url&&url.url||url);
  if(!u.includes('/api/chat-assist'))return __fa.apply(this,arguments);
  const b=JSON.parse((opts&&opts.body)||'{}'), j=o=>Promise.resolve(new Response(JSON.stringify(o),{status:200,headers:{'Content-Type':'application/json'}}));
  if(b.probe)return j({ok:true,allowed:__assist.allowed});
  __assist.calls.push(b);
  if(b.action==='reply')return j({ok:true,content:CHOICES[0],choices:CHOICES});
  if(b.action==='summarize')return j({ok:true,summary:'- Alice asked about Thursday\n- Waiting on you: say if you can come'});
  return j({ok:true,links:[{url:'https://youtu.be/abc',title:'A very long video title that has to wrap on a phone screen somehow',summary:'The video explains the plan.'},
                           {url:'http://192.168.0.1/x',title:'http://192.168.0.1/x',error:'URL blocked: private address'}]});
};
""".replace("CHOICES", json.dumps(CHOICES))

MENU = "[...document.querySelectorAll('.menu-pop button[data-m]')].map(b=>b.textContent.trim())"
BOX = r"""(sel=>{const e=document.querySelector(sel);if(!e)return null;const r=e.getBoundingClientRect();
  return {l:r.left,r:r.right,t:r.top,b:r.bottom,w:r.width,h:r.height,vw:innerWidth,vh:innerHeight,hidden:e.hidden||r.width===0}})"""


def _fits(r):
    return r and not r['hidden'] and r['l'] >= -0.5 and r['r'] <= r['vw'] + 0.5 and r['b'] <= r['vh'] + 0.5


async def _pick(b, label):
    await b.until("!!document.querySelector('.menu-pop button[data-m]')")
    await asyncio.sleep(.35)                                 # a phone's menu slides up
    await b.js(f"[...document.querySelectorAll('.menu-pop button[data-m]')].find(x=>x.textContent.includes({json.dumps(label)})).click()")


async def _exercise(b, btn, text_sel, sent_js, neighbour):
    got = {}
    await b.until(f"!!document.querySelector('{btn}') && !document.querySelector('{btn}').hidden")
    got['button'] = await b.js(BOX + f"('{btn}')")
    got['neighbour'] = await b.js(BOX + f"('{neighbour}')")
    # Dismiss the first menu: the ✨ must still work afterwards.
    await b.js(f"document.querySelector('{btn}').click()")
    await b.until("!!document.querySelector('.menu-pop')")
    got['menu'] = await b.js(MENU)
    await b.js("document.body.dispatchEvent(new MouseEvent('click',{bubbles:true}))")
    await b.until("!document.querySelector('.menu-pop')")
    await asyncio.sleep(.4)
    # Generate reply → three drafts → the second fills the composer, unsent.
    await b.js(f"document.querySelector('{btn}').click()")
    await _pick(b, 'Generate reply')
    await b.until("__assist.calls.length===1 && !!document.querySelector('.menu-pop .ca-choice')")
    await asyncio.sleep(.35)
    got['choices'] = await b.js(MENU)
    got['choice_pop'] = await b.js(BOX + "('.menu-pop')")
    await b.js("document.querySelectorAll('.menu-pop button[data-m]')[1].click()")
    await b.until(f"document.querySelector('{text_sel}').value.length>0")
    got['composer'] = await b.js(f"document.querySelector('{text_sel}').value")
    got['sent'] = await b.js(sent_js)
    got['reply_call'] = await b.js("__assist.calls[0]")
    # Summarize chat → a sheet, readable, Copy copies the summary.
    await b.until(f"!document.querySelector('{btn}').disabled")
    await b.js(f"document.querySelector('{btn}').click()")
    await _pick(b, 'Summarize chat')
    await b.until("!!document.querySelector('.ca-sheet .ca-line')")
    got['summary'] = await b.js("[...document.querySelectorAll('.ca-sheet .ca-line')].map(l=>l.textContent)")
    got['sheet'] = await b.js(BOX + "('.ca-sheet')")
    await b.js("window.__copied=[];__PC.copyValue=(t)=>{__copied.push(t);return Promise.resolve(true)};document.querySelector('.ca-copy').click()")
    got['copied'] = await b.js("__copied")
    await b.js("document.querySelector('.ca-close').click()")
    await b.until("!document.querySelector('.ca-sheet')")
    # Summarize YouTube & links → each link, its summary or the sentence saying why not.
    await b.js(f"document.querySelector('{btn}').click()")
    await _pick(b, 'YouTube & links')
    await b.until("!!document.querySelector('.ca-sheet .ca-item')")
    got['links'] = await b.js("[...document.querySelectorAll('.ca-sheet .ca-item')].map(i=>({href:(i.querySelector('a')||{}).href||'',text:i.textContent}))")
    got['link_sheet'] = await b.js(BOX + "('.ca-sheet')")
    got['actions'] = [c['action'] for c in await b.js("__assist.calls")]
    return got


def _assert_common(got, medium):
    # The app zooms itself by window width, so the ✨ is compared with the control beside it.
    assert _fits(got['button']) and abs(got['button']['h'] - got['neighbour']['h']) < 1 and got['button']['w'] >= got['neighbour']['h'] * .6, (got['button'], got['neighbour'])
    assert got['menu'] == ['✍️ Generate reply', '📝 Summarize chat', '▶️ Summarize YouTube & links'], got['menu']
    assert got['choices'] == CHOICES, got['choices']
    assert _fits(got['choice_pop']), got['choice_pop']
    assert got['composer'] == CHOICES[1] and got['sent'] == 0, got
    call = got['reply_call']
    assert call['action'] == 'reply' and call['medium'] == medium and call['count'] == 3, call
    assert got['summary'] == ['- Alice asked about Thursday', '- Waiting on you: say if you can come']
    assert _fits(got['sheet']) and _fits(got['link_sheet']), (got['sheet'], got['link_sheet'])
    assert got['copied'] == ['- Alice asked about Thursday\n- Waiting on you: say if you can come']
    assert got['links'][0]['href'] == 'https://youtu.be/abc' and 'The video explains the plan.' in got['links'][0]['text']
    assert 'URL blocked' in got['links'][1]['text'] and got['links'][1]['href'] == ''
    assert got['actions'] == ['reply', 'summarize', 'links']


TG_LINK = "__tgMsgs.push({id:4, chat_id:42, out:false, date:1700000004, text:'watch https://youtu.be/abc', sender:'Alice', sender_id:7, reply_to:0, media:null});"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 820])
def test_telegram_sparkle(width):
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await _open_tg(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("document.querySelectorAll('.tg-msg').length===4")
        got.update(await _exercise(b, '.tg-composer [data-act=ai]', '.tg-text', '__tg.posts.length+__tg.files.length', '.tg-composer [data-act=attach]'))
        # The ✨ is in the composer row with the other controls.
        got['row'] = await b.js("[...document.querySelectorAll('.tg-composer > button:not([hidden])')].map(x=>x.dataset.act)")
    extra = FAKE.replace("state:'none'", "state:'ready'") + TG_LINK + ASSIST
    asyncio.run(desktop.with_browser("online", "", check, extra_init=extra))
    _assert_common(got, 'telegram')
    msgs = got['reply_call']['messages']
    assert msgs[0] == {'me': False, 'text': 'hey there', 'who': 'Alice'}, msgs
    assert msgs[1]['text'] == '[a photo]' and msgs[2] == {'me': True, 'text': 'the doc', 'who': ''}, msgs
    # The PosterChan emoji picker joined the row (2026-10-09); ✨ still sits beside Send.
    assert got['row'] == ['attach', 'camera', 'emoji', 'ai', 'send'], got['row']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_telegram_has_no_sparkle_without_ai():
    got = {}

    async def check(b):
        await _open_tg(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("document.querySelectorAll('.tg-msg').length===3")
        await asyncio.sleep(.8)
        got['hidden'] = await b.js("(()=>{const x=document.querySelector('.tg-composer [data-act=ai]');return !x||x.hidden||x.getBoundingClientRect().width===0})()")
    extra = FAKE.replace("state:'none'", "state:'ready'") + ASSIST + "__assist.allowed=false;"
    asyncio.run(desktop.with_browser("online", "", check, extra_init=extra))
    assert got['hidden'] is True


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('phone', [True, False])
def test_dm_sparkle(phone):
    got = {}

    async def check(b):
        w, h = (390, 844) if phone else (1280, 900)
        await b.call('Emulation.setDeviceMetricsOverride', {'width': w, 'height': h, 'deviceScaleFactor': 1, 'mobile': phone})
        await desktop.login(b)
        pk = await b.js(DM_SEED + '(6)')
        # One incoming message with a link, as the peer wrote it.
        await b.js(r'''(()=>{const me=new Uint8Array(32).fill(1),mePk=NostrTools.getPublicKey(me),peer=new Uint8Array(32).fill(7);
          const t=Math.floor(Date.now()/1000)-60,{createRumor,createSeal}=NostrTools.nip59;
          const seal=createSeal(createRumor({kind:14,created_at:t,tags:[['p',mePk]],content:'watch https://youtu.be/abc'},peer),peer,mePk);
          const eph=NostrTools.generateSecretKey();
          const r=_rel();r.push(NostrTools.finalizeEvent({kind:1059,created_at:t,tags:[['p',mePk]],
            content:NostrTools.nip44.encrypt(JSON.stringify(seal),NostrTools.nip44.getConversationKey(eph,mePk))},eph));
          localStorage.setItem('__relayEvents',JSON.stringify(r));})()''')
        await b.js("__PC.switchView('messages')")
        sel = "#dm-rows .dm-peer[data-peer=" + json.dumps(pk) + "]"
        await b.until(f"!!document.querySelector('{sel}')")
        await b.js(f"document.querySelector('{sel}').click()")
        await b.until("!!document.querySelector('#dm-msgs') && document.querySelector('#dm-msgs').textContent.includes('youtu.be/abc')")
        await b.js("window.__dmSent=0;")
        got.update(await _exercise(b, '#dm-ai', '#dm-in', "document.querySelectorAll('#dm-msgs .bubble.out').length", '#dm-attach'))
        got['row'] = await b.js(r"""(()=>{const r=[...document.querySelectorAll('.dm-row > *')].filter(e=>e.getBoundingClientRect().width>0)
            .map(e=>{const x=e.getBoundingClientRect();return {id:e.id,l:x.left,r:x.right,t:x.top,b:x.bottom}});return {r,vw:innerWidth}})()""")
    asyncio.run(desktop.with_browser('online', '', check, PLAIN + RELAY + ASSIST))
    _assert_common(got, 'dm')
    msgs = got['reply_call']['messages']
    assert msgs[-1] == {'me': False, 'text': 'watch https://youtu.be/abc'}, msgs[-3:]
    assert all(set(m) == {'me', 'text'} for m in msgs), 'a DM sends no names'
    for e in got['row']['r']:
        assert e['l'] >= -0.5 and e['r'] <= got['row']['vw'] + 0.5, ('the composer row overflows', e)
    ids = [e['id'] for e in got['row']['r']]
    assert 'dm-ai' in ids and ids.index('dm-ai') < ids.index('dm-in'), ids


TG_ARTICLE = "__tgMsgs.push({id:5, chat_id:42, out:true, date:1700000005, text:'read this https://example.com/story', sender:'', sender_id:1, reply_to:0, media:null});"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [1280, 820])
def test_telegram_link_messages_carry_their_own_sparkle(width):
    """"we need sparkle on links and youtube links ... to summarize". Each message with a link gets
    ✨ Summarize video / link under it; a tap summarizes THAT message's links only."""
    got = {}

    async def check(b):
        await b.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        await _open_tg(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("document.querySelectorAll('.tg-msg').length===5 && document.querySelectorAll('.tg-sum').length===2")
        got['chips'] = await b.js("[...document.querySelectorAll('.tg-msg')].map(m=>[m.dataset.id,(m.querySelector('.tg-sum')||{}).textContent||null])")
        got['fits'] = await b.js("[...document.querySelectorAll('.tg-sum')].every(c=>{const r=c.getBoundingClientRect(),m=c.closest('.tg-msg').getBoundingClientRect();return r.width>0&&r.right<=m.right+1&&r.left>=m.left-1})")
        await b.js("document.querySelector('.tg-msg[data-id=\"4\"] .tg-sum').click()")
        await b.until("!!document.querySelector('.ca-sheet .ca-item')")
        got['call'] = await b.js("__assist.calls[0]")
        got['sheet'] = await b.js("document.querySelector('.ca-sheet').textContent")
    extra = FAKE.replace("state:'none'", "state:'ready'") + TG_LINK + TG_ARTICLE + ASSIST
    asyncio.run(desktop.with_browser("online", "", check, extra_init=extra))
    chips = dict((i, t) for i, t in got['chips'])
    assert chips['4'].strip() == '✨ Summarize video' and chips['5'].strip() == '✨ Summarize link', got['chips']
    assert chips['1'] is None and chips['3'] is None, "a message with no link got a sparkle"
    assert got['fits'], "a sparkle spills out of its message"
    assert got['call']['action'] == 'links' and got['call']['medium'] == 'telegram'
    assert got['call']['messages'] == [{'me': False, 'text': 'watch https://youtu.be/abc'}], got['call']
    assert 'The video explains the plan.' in got['sheet']


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_no_link_sparkles_without_ai():
    got = {}

    async def check(b):
        await _open_tg(b)
        await b.until("document.querySelectorAll('.tg-dialog').length===2")
        await b.js("document.querySelector('.tg-dialog[data-chat=\"42\"]').click()")
        await b.until("document.querySelectorAll('.tg-msg').length===4")
        await asyncio.sleep(.8)
        got['chips'] = await b.js("document.querySelectorAll('.tg-sum').length")
    extra = FAKE.replace("state:'none'", "state:'ready'") + TG_LINK + ASSIST + "__assist.allowed=false;"
    asyncio.run(desktop.with_browser("online", "", check, extra_init=extra))
    assert got['chips'] == 0
