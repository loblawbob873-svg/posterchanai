"""A sender with no digits -- "AIS", "Google" -- is its own conversation, and it opens, archives and deletes.

Reported: "there is AIS messages that can never be archived or deleted". The conversation key kept only
the DIGITS of an address (`key()`, SmsKeys.matchKey's twin), so "AIS" became "" -- every alphanumeric
sender folded into ONE keyless thread: archiving it was refused (setArchived needs a key: "could not
archive"), and it could not be opened to delete a message, because an open conversation of "" reads as
"none open". Conversations are now grouped by convKey (`a:ais`); message identity (docId) is unchanged.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_sms_archive_conversations_full_app import PLAIN, ROWS, _open_row_menu
from tests.client.test_sms_image_paste_full_app import open_texts


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SEED = r"""
(()=>{
  const S=PCSms._state(), now=Date.now();
  S.msgs.clear();
  const add=(doc,addr,body,ago)=>S.msgs.set(doc,{doc,address:addr,body,date:now-ago,incoming:true,parts:[],_at:1});
  add('ais-1','AIS','AIS: your data package expires today',3600000);
  add('ais-2','AIS','AIS: top up 100 THB for 30 days',60000);
  add('ggl-1','Google','G-123456 is your Google verification code',120000);
  add('num-1','+15550199','dinner tonight?',180000);
  S.open='';S.q='';S.archived.clear();S.showArchived=false;
  window.published=[];
  __PC.publish=async(kind,content,tags)=>{
    const ev={kind,content,tags,created_at:Math.floor(Date.now()/1000)+published.length,pubkey:__PC.ME.pubkey,id:'x'+published.length};
    published.push(ev); return {ok:true,ev};
  };
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


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('phone', [True, False])
def test_an_alphanumeric_sender_is_its_own_conversation_and_opens(phone):
    got = {}

    async def check(b):
        await _texts(b, phone)
        got['rows'] = await b.js(ROWS)
        await b.js("[...document.querySelectorAll('.sms-thread')].find(b=>b.textContent.includes('top up 100')).click()")
        await asyncio.sleep(.8)
        got['bubbles'] = await b.js("[...document.querySelectorAll('.bubble[data-doc]')].map(x=>x.dataset.doc).sort()")

    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
    assert sorted(got['rows']) == sorted(['AIS: top up 100 THB for 30 days', 'G-123456 is your Google verification code',
                                          'dinner tonight?']), ('AIS and Google must be two conversations', got['rows'])
    assert got['bubbles'] == ['ais-1', 'ais-2'], ('the AIS conversation does not open', got)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_alphanumeric_conversation_archives():
    got = {}

    async def check(b):
        await _texts(b, False)
        await _open_row_menu(b, False, 'top up 100')
        await b.js("document.querySelector('.menu-pop button[data-m]').click()")
        await asyncio.sleep(1)
        got['rows'] = await b.js(ROWS)
        got['toasts'] = await b.js("(window.toasts||[]).slice(-3)")
        ev = await b.js("published[0]||null")
        got['rec'] = ev and await b.js(f"__PC.nip44dec(__PC.ME.pubkey,{json.dumps(ev['content'])}).then(JSON.parse)")

    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
    assert got['rec'] and got['rec']['k'] == 'a:ais' and got['rec']['upto'] > 0, got
    assert not any('top up' in r for r in got['rows']), ('archived, still listed', got)
    assert any('Google' in r for r in got['rows']), ('archiving AIS took another sender with it', got)


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_message_from_an_alphanumeric_sender_deletes():
    got = {}

    async def check(b):
        await _texts(b, False)
        await b.js("[...document.querySelectorAll('.sms-thread')].find(b=>b.textContent.includes('top up 100')).click()")
        await b.until("document.querySelectorAll('.bubble[data-doc]').length===2")
        await b.js("__PC.uiConfirm=async()=>true")
        await b.js("document.querySelector('.bubble[data-doc=\"ais-1\"]').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}))")
        # Right-click opens the message menu (reactions + Copy + Delete); Delete is in it.
        await b.until("!!document.querySelector('.sms-msg-actions')")
        await b.js("[...document.querySelectorAll('.sms-msg-actions button')].find(x=>x.textContent.trim()==='Delete').click()")
        await asyncio.sleep(1.2)
        got['left'] = await b.js("[...document.querySelectorAll('.bubble[data-doc]')].map(x=>x.dataset.doc)")
        got['gone'] = await b.js("!!(PCSms._state().msgs.get('ais-1')||{gone:true}).gone")

    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
    assert got['gone'] and got['left'] == ['ais-2'], got
