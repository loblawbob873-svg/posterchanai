"""Texts: long-press a message for the phone-style reaction bar, and other phones' reactions show as
reactions ("text messages on android and web/desktop need that reaction thing that Android and iphone do
when long-pressing on the message" / "we need to make sure we support their reactions too").

The shipped Texts screen, at phone (touch long-press) and desktop (right-click) size:
  * an iPhone/Google any-emoji reaction ("Reacted 🔥 to “sure”") and a picture reaction ("Liked a photo")
    are drawn as 🔥 / 👍 under the message they are about, never as their own text bubbles;
  * long-press / right-click opens the six reactions over the message and Copy / Delete under it;
  * ❤️ asks for exactly the interoperable "Loved “…”" text, and Delete still deletes.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_sms_archive_conversations_full_app import PLAIN
from tests.client.test_sms_image_paste_full_app import open_texts


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SEED = r"""
(()=>{
  const S=PCSms._state(), now=Date.now(), A='+15550199';
  S.msgs.clear();
  const add=(doc,body,ago,incoming,parts)=>S.msgs.set(doc,{doc,address:A,body,date:now-ago,incoming,parts:parts||[],_at:1});
  add('in-1','dinner tonight?',600000,true);
  add('out-1','sure',500000,false);
  add('in-2','Reacted 🔥 to “sure”',400000,true);
  add('out-2','',300000,false,[{ct:'image/jpeg',name:'pic.jpg',size:10}]);
  add('in-3','Liked a photo',200000,true);
  S.ready=true;S.open='';S.q='';S.archived.clear();S.showArchived=false;
  window.published=[];
  __PC.publish=async(kind,content,tags)=>{
    const ev={kind,content,tags,created_at:Math.floor(Date.now()/1000)+published.length,pubkey:__PC.ME.pubkey,id:'x'+published.length};
    published.push(ev); return {ok:true,ev};
  };
  window.__formats=[];
  const f=PCSmsReactions.format;PCSmsReactions.format=(k,r,t)=>{__formats.push([k,r,t]);return f(k,r,t);};
  PCSms.refreshNames();
})()
"""


async def _texts(b, phone):
    w, h = (390, 844) if phone else (1280, 900)
    await b.call('Emulation.setDeviceMetricsOverride', {'width': w, 'height': h, 'deviceScaleFactor': 1, 'mobile': phone})
    if phone:
        await b.call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
    await open_texts(b)
    await b.js(SEED)
    await asyncio.sleep(.6)
    await b.js("[...document.querySelectorAll('.sms-thread')].find(b=>b.textContent.includes('photo')||b.textContent.includes('Liked')||b.textContent.includes('dinner')).click()")
    await b.until("document.querySelectorAll('.bubble[data-doc]').length>=2")
    await asyncio.sleep(.4)


async def _hold(b, doc, phone):
    sel = f"document.querySelector('.bubble[data-doc=\"{doc}\"]')"
    if phone:
        await b.js(f"(()=>{{const el={sel};const r=el.getBoundingClientRect();"
                   "el.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerType:'touch',clientX:r.left+10,clientY:r.top+10}))})()")
        await asyncio.sleep(.7)
    else:
        await b.js(f"{sel}.dispatchEvent(new MouseEvent('contextmenu',{{bubbles:true,cancelable:true}}))")
    await b.until("!!document.querySelector('.sms-react-pick') || !!document.querySelector('.sms-msg-actions')")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('phone', [True, False])
def test_long_press_reacts_and_other_phones_reactions_are_reactions(phone):
    got = {}

    async def check(b):
        await _texts(b, phone)
        got['bubbles'] = await b.js("[...document.querySelectorAll('.bubble[data-doc]')].map(x=>x.dataset.doc).sort()")
        got['chips'] = await b.js("Object.fromEntries([...document.querySelectorAll('.bubble[data-doc]')].map(x=>[x.dataset.doc,(x.querySelector('.sms-reacts')||{}).textContent||'']))")
        await _hold(b, 'in-1', phone)
        R = "(s=>{const n=document.querySelector(s);if(!n)return null;const r=n.getBoundingClientRect();return {t:r.top,b:r.bottom,h:r.height}})"
        got['bar'] = await b.js(f"({{n:document.querySelectorAll('.sms-react-pick button').length, bar:{R}('.sms-react-pick'), msg:{R}('.bubble[data-doc=\"in-1\"]'), menu:[...document.querySelectorAll('.sms-msg-actions button')].map(x=>x.textContent.trim())}})")
        await b.js("document.querySelector('.sms-react-pick button[data-kind=heart]').click()")
        await asyncio.sleep(.5)
        got['formats'] = await b.js("__formats.filter(f=>f[0]==='heart')")
        got['closed'] = await b.js("!document.querySelector('.sms-react-pick') && !document.querySelector('.sms-msg-actions')")
        # Delete from the long-press menu still deletes (after its confirmation).
        await b.js("window.__confirms=[];__PC.uiConfirm=async(m)=>{__confirms.push(String(m));return true}")
        await _hold(b, 'in-1', phone)
        await b.js("[...document.querySelectorAll('.sms-msg-actions button')].find(x=>x.textContent.trim()==='Delete').click()")
        await asyncio.sleep(1.2)
        got['gone'] = await b.js("!!(PCSms._state().msgs.get('in-1')||{gone:true}).gone")
        got['dbg'] = await b.js("({confirms:__confirms, toasts:(window.toasts||[]).slice(-4), menu:!!document.querySelector('.sms-msg-actions'), msg:JSON.stringify(PCSms._state().msgs.get('in-1')||null).slice(0,200)})")

    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
    assert 'in-2' not in got['bubbles'] and 'in-3' not in got['bubbles'], ("another phone's reaction drawn as a text bubble", got['bubbles'])
    assert '🔥' in got['chips'].get('out-1', ''), ("iPhone/Google 'Reacted 🔥 to' not shown on the message", got['chips'])
    assert '👍' in got['chips'].get('out-2', ''), ("'Liked a photo' not shown on the picture", got['chips'])
    bar = got['bar']
    assert bar['n'] == 6, bar
    assert bar['bar'] and bar['msg'] and (bar['bar']['b'] <= bar['msg']['t'] + 1 or bar['bar']['t'] >= bar['msg']['b'] - 1), ("the bar covers the message", bar)
    assert bar['menu'] == ['Copy text', 'Delete'], bar
    assert got['formats'] == [['heart', False, 'dinner tonight?']], got['formats']
    assert got['closed'], "the bar stayed open after reacting"
    assert got['gone'], ("Delete from the long-press menu did not delete", got['dbg'])
