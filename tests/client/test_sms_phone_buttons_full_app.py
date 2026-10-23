"""Texts: every button that talks to the phone does what it says, pressed in the REAL bundled renderer.

The Android bridge is the only fixture: `__PC.capPlugin` hands back a recording stand-in for the Sms
and HomeScreen plugins, so each press is checked by the native call it makes (and its arguments) and
by what the screen then says -- never merely by the button existing.

  * #sms-new asks for a number and opens that conversation;
  * #sms-defaults / #sms-defaults2 open Android's Default apps screen;
  * #sms-allow / #sms-allow2 ask Android for READ_SMS, say a refusal out loud, read the phone on a yes;
  * #sms-deep / #sms-deep2 page the phone's history backwards and say how it went;
  * #sms-now copies the phone's messages across (it had no handler at all);
  * #sms-archive-retry retries a stopped backup (the "Retry now" copy had no handler) and re-offers
    refused attachments;
  * #sms-block asks before blocking, and Unblock does not;
  * #sms-emoji inserts the picked emoji into the draft.
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_sms_image_paste_full_app import open_texts


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLAIN = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"

CHROME = pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')

# A recording Android bridge. Every method of every plugin answers; `resp['Plugin.method']` is the
# answer (a value or a function of the arguments), `calls` is every call in order with its argument.
BRIDGE = r"""
(()=>{
  window.calls=[];window.resp={};
  const plugin=name=>new Proxy({},{get:(_,m)=>{
    if(m==='then'||typeof m!=='string') return undefined;
    return async(...a)=>{
      calls.push([name,m,a[0]===undefined?null:a[0]]);
      const r=resp[name+'.'+m];
      return typeof r==='function'?r(...a):(r===undefined?{}:r);
    };
  }});
  __PC.capPlugin=(name)=>plugin(name);
  __PC.publish=async(kind,content,tags)=>({ok:true,ev:{kind,content,tags,id:'p'+Math.random(),created_at:Math.floor(Date.now()/1000)}});
})()
"""

PHONE = "{isDefault:%s,canRead:%s,package:'place.poster.app',defaultPackage:%s,roleHeld:false,telephony:true,mms:true,mmsFetch:true,canNotify:true}"


def status(is_default, can_read):
    return PHONE % ('true' if is_default else 'false', 'true' if can_read else 'false',
                    "'place.poster.app'" if is_default else "'com.google.android.apps.messaging'")


SEED = r"""
(()=>{
  const S=PCSms._state(), now=Date.now();
  S.msgs.clear();
  S.msgs.set('pb-1',{doc:'pb-1',address:'+15550100',body:'are you coming',date:now-60000,incoming:true,parts:[],_at:1});
  S.open='';S.q='';S.archived.clear();S.showArchived=false;S.emptyWhy='';S.emptyFix='';
  PCSms.refreshNames();
})()
"""

EMPTY = r"""
((why,fix)=>{
  const S=PCSms._state();
  S.msgs.clear();S.open='';S.q='';S.emptyWhy=why;S.emptyFix=fix;
  PCSms.refreshNames();
})"""

VISIBLE = "(sel=>{const e=document.querySelector(sel);return !!e&&!!e.offsetParent})"
N = "(name,m)=>calls.filter(c=>c[0]===name&&c[1]===m).length"


async def _list(b, st, seed=True):
    await open_texts(b)
    await b.js(BRIDGE)
    await b.js(f"resp['Sms.status']={st}")
    if seed:
        await b.js(SEED)
        await b.until("document.querySelectorAll('.sms-thread').length===1")


async def _count(b, plugin, method):
    return await b.js(f"({N})('{plugin}','{method}')")


@CHROME
def test_new_asks_for_a_number_and_opens_that_conversation():
    async def check(b):
        await _list(b, status(True, True))
        await b.js("document.querySelector('#sms-new').click()")
        await b.until("!!document.querySelector('.uiprompt-in')")
        assert await b.js("document.querySelector('.uiconfirm-msg').textContent") == 'Phone number'
        # Cancel: nothing opens.
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"0\"]').click()")
        await b.until("!document.querySelector('.uiprompt-in')")
        assert await b.js("PCSms._state().open") == ''
        assert await b.js("!!document.querySelector('#sms-in')") is False

        await b.js("document.querySelector('#sms-new').click()")
        await b.until("!!document.querySelector('.uiprompt-in')")
        await b.js("document.querySelector('.uiprompt-in').value='+15550177';"
                   "document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click()")
        await b.until("!!document.querySelector('#sms-in')")
        assert await b.js("document.querySelector('.sms-title').textContent.trim()") == '+15550177'
        assert await b.js("PCSms._state().open") == await b.js("PCSms._convKey('+15550177')")
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))


@CHROME
def test_default_apps_buttons_open_androids_default_apps_screen():
    async def check(b):
        # A phone that can read but is not the messages app: the role bar is offered.
        await _list(b, status(False, True))
        await b.until(VISIBLE + "('#sms-defaults2')")
        assert await _count(b, 'HomeScreen', 'openDefaultApps') == 0
        await b.js("document.querySelector('#sms-defaults2').click()")
        await b.until(f"({N})('HomeScreen','openDefaultApps')===1")

        # The empty-state copy, offered with the role sentence.
        await b.js(EMPTY + "('Android says this phone\\u2019s messages app is someone else','role')")
        await b.until(VISIBLE + "('#sms-defaults')")
        await b.js("document.querySelector('#sms-defaults').click()")
        await b.until(f"({N})('HomeScreen','openDefaultApps')===2")
        assert await _count(b, 'HomeScreen', 'requestSms') == 0, 'Default apps is not the role dialog'
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))


REFUSED = 'Android is not offering the prompt any more'


@CHROME
def test_allow_under_the_header_asks_android_and_says_a_refusal_out_loud():
    async def check(b):
        # An older copy is on screen and this phone may not read: the header offers the permission.
        await _list(b, status(False, False))
        await b.until(VISIBLE + "('#sms-allow2')")
        await b.js("resp['Sms.ensureRead']={granted:false}")
        await b.js("document.querySelector('#sms-allow2').click()")
        await b.until(f"({N})('Sms','ensureRead')===1")
        # The refusal is SAID, even with messages on screen (the empty state that holds it is not drawn).
        await b.until(f"toasts.some(t=>t.includes({REFUSED!r}))")
        assert await b.js("document.querySelector('#sms-allow2').disabled") is False

        # Granted: the phone is read at once, and the offer goes away.
        await b.js(f"resp['Sms.ensureRead']={{granted:true}};resp['Sms.status']={status(False, True)}")
        lists = await _count(b, 'Sms', 'list')
        await b.js("document.querySelector('#sms-allow2').click()")
        await b.until(f"({N})('Sms','ensureRead')===2 && ({N})('Sms','list')>{lists}")
        await b.until("!(" + VISIBLE + "('#sms-allow2'))")
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))


@CHROME
def test_allow_in_the_empty_state_asks_android_and_reads_the_phone():
    async def check(b):
        await _list(b, status(False, False), seed=False)
        await b.js(EMPTY + "('PosterChan has not been allowed to read this phone\\u2019s messages yet.','perm')")
        await b.until(VISIBLE + "('#sms-allow')")
        await b.js("resp['Sms.ensureRead']={granted:false}")
        await b.js("document.querySelector('#sms-allow').click()")
        await b.until(f"document.querySelector('.sms-empty').textContent.includes({REFUSED!r})")
        assert await _count(b, 'Sms', 'ensureRead') == 1
        assert await b.js("!!document.querySelector('#sms-allow')") is False, 'the dead offer is withdrawn'

        await b.js(f"resp['Sms.ensureRead']={{granted:true}};resp['Sms.status']={status(True, True)}")
        await b.js(EMPTY + "('PosterChan has not been allowed to read this phone\\u2019s messages yet.','perm')")
        await b.until(VISIBLE + "('#sms-allow')")
        lists = await _count(b, 'Sms', 'list')
        await b.js("document.querySelector('#sms-allow').click()")
        await b.until(f"({N})('Sms','ensureRead')===2 && ({N})('Sms','list')>{lists}")
        await b.until("!document.querySelector('#sms-allow')")
        assert 'not been allowed' not in await b.js("document.querySelector('.sms-empty').textContent")
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))


@CHROME
@pytest.mark.parametrize('where', ['header', 'empty'])
def test_bring_in_older_messages_pages_the_phone_backwards_and_says_how_it_went(where):
    async def check(b):
        await _list(b, status(True, True), seed=(where == 'header'))
        if where == 'empty':
            await b.js(EMPTY + "('No messages on this phone.','deep')")
        btn = '#sms-deep2' if where == 'header' else '#sms-deep'
        await b.until(VISIBLE + f"('{btn}')")
        await b.js("resp['Sms.list']=()=>new Promise(r=>{window.releaseList=()=>r({messages:[]})})")
        await b.js(f"document.querySelector('{btn}').click()")
        await b.until("typeof releaseList==='function'")
        call = await b.js("calls.filter(c=>c[0]==='Sms'&&c[1]==='list').pop()[2]")
        assert set(call) == {'before', 'limit'} and call['limit'] == 400, call
        assert await b.js("document.querySelector('#sms-deep-note').textContent") == 'reading…'
        await b.js("releaseList()")
        await b.until("document.querySelector('#sms-deep-note').textContent==='nothing older found'")
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))


@CHROME
def test_copy_my_messages_across_runs_the_copy():
    async def check(b):
        await _list(b, status(True, True), seed=False)
        await b.js("resp['Sms.list']={messages:[]}")
        await b.js(EMPTY + "('Nothing has been copied across yet. The first pass reaches back 30 days.','mirror')")
        await b.until(VISIBLE + "('#sms-now')")
        lists = await _count(b, 'Sms', 'list')
        await b.js("document.querySelector('#sms-now').click()")
        await b.until(f"({N})('Sms','list')>{lists}")
        call = await b.js("calls.filter(c=>c[0]==='Sms'&&c[1]==='list')[%d][2]" % lists)
        assert 'since' in call and call['since'] > 0, call
        # Copied: the screen no longer claims nothing has been copied, and the button is gone.
        await b.until("!document.querySelector('#sms-now')")
        assert await b.js("document.querySelector('.sms-empty').textContent") == 'No messages on this phone.'
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))


@CHROME
def test_archive_retry_restarts_a_stopped_backup_and_reoffers_refused_pictures():
    async def check(b):
        await _list(b, status(True, True))
        await b.js("resp['Sms.list']={messages:[]}")
        await b.js("PCSms._state().archive.error='the relay refused';PCSms.refreshNames()")
        await b.until("!!document.querySelector('#sms-archive-retry')")
        assert await b.js("document.querySelector('#sms-archive-retry').textContent") == 'Retry now'
        lists = await _count(b, 'Sms', 'list')
        await b.js("document.querySelector('#sms-archive-retry').click()")
        await b.until(f"({N})('Sms','list')>{lists}")
        assert 'since' in await b.js("calls.filter(c=>c[0]==='Sms'&&c[1]==='list')[%d][2]" % lists)
        # The pass ran to its end and the stale stoppage is no longer on screen.
        await b.until("!PCSms._state().archive.running && PCSms._state().archive.error!=='the relay refused'")
        await b.until("!document.querySelector('#sms-archive').textContent.includes('the relay refused')")

        # Refused attachments: "Try those again" re-reads the phone.
        await b.js("const S=PCSms._state();S.archive.error='';S.archive.refused=2;PCSms.refreshNames()")
        await b.until("!!document.querySelector('#sms-archive-retry') && "
                      "document.querySelector('#sms-archive-retry').textContent==='Try those again'")
        assert '2 picture messages had no attachment' in await b.js("document.querySelector('#sms-archive').textContent")
        lists = await _count(b, 'Sms', 'list')
        await b.js("document.querySelector('#sms-archive-retry').click()")
        await b.until(f"({N})('Sms','list')>{lists}")
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))


@CHROME
def test_block_asks_first_and_unblock_does_not():
    async def check(b):
        await _list(b, status(True, True))
        # The phone's blocked-numbers list does what it is asked.
        await b.js("resp['Sms.setBlocked']=a=>({blocked:a.block})")
        await b.js("document.querySelector('.sms-thread').click()")
        await b.until("!!document.querySelector('#sms-block')")
        assert await b.js("document.querySelector('#sms-block').textContent") == 'Block'
        k = await b.js("PCSms._key('+15550100')")

        # Cancel: nothing blocked.
        await b.js("document.querySelector('#sms-block').click()")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        assert 'Block +15550100?' in await b.js("document.querySelector('.uiconfirm-msg').textContent")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"0\"]').click()")
        await b.until("!document.querySelector('.uiconfirm-btns')")
        assert await b.js(f"!!PCSms._state().blocks[{k!r}]") is False

        # OK: blocked, the phone's list is asked to apply it, and the header now offers Unblock.
        await b.js("document.querySelector('#sms-block').click()")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        await b.js("document.querySelector('.uiconfirm-btns [data-uc=\"1\"]').click()")
        await b.until("document.querySelector('#sms-block')&&document.querySelector('#sms-block').textContent==='Unblock'")
        assert await b.js(f"PCSms._state().blocks[{k!r}].block") is True
        assert await b.js("PCSms.isBlocked('+15550100')")
        await b.until("toasts.includes('Blocked on your phone')")
        assert await b.js("calls.filter(c=>c[1]==='setBlocked').map(c=>c[2])") == [{'number': '+15550100', 'block': True}]

        # Unblock: no question asked.
        await b.js("document.querySelector('#sms-block').click()")
        await b.until("document.querySelector('#sms-block').textContent==='Block'")
        assert await b.js("!!document.querySelector('.uiconfirm-btns')") is False
        assert await b.js(f"PCSms._state().blocks[{k!r}].block") is False
        await b.until("toasts.includes('Unblocked')")
        assert (await b.js("calls.filter(c=>c[1]==='setBlocked').map(c=>c[2])"))[-1] == {'number': '+15550100', 'block': False}
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))


@CHROME
def test_emoji_button_inserts_the_picked_emoji_into_the_draft():
    async def check(b):
        await _list(b, status(True, True))
        await b.js("document.querySelector('.sms-thread').click()")
        await b.until("!!document.querySelector('#sms-emoji')")
        await b.js("const i=document.querySelector('#sms-in');i.value='hi ';i.dispatchEvent(new Event('input'));"
                   "i.focus();i.setSelectionRange(3,3)")
        await b.js("document.querySelector('#sms-emoji').click()")
        await b.until("!!document.querySelector('.emoji-pop .ep-grid button[data-e]')")
        e = await b.js("document.querySelector('.emoji-pop .ep-grid button[data-e]').dataset.e")
        await b.js("document.querySelector('.emoji-pop .ep-grid button[data-e]')"
                   ".dispatchEvent(new MouseEvent('mousedown',{bubbles:true,cancelable:true}))")
        await b.until("!document.querySelector('.emoji-pop')")
        assert await b.js("document.querySelector('#sms-in').value") == 'hi ' + e
        # Kept as the conversation's draft, so a repaint cannot lose it.
        assert await b.js("PCSms._state().draft[PCSms._state().open].text") == 'hi ' + e
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
