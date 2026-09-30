"""Texts: archive a conversation, on every device, until a new message brings it back.

Asked for: "SMS need way to archive old conversations that is consistent across all your devices. They
can come back if they receive a new message". Driven in the REAL bundled Texts renderer at phone and
desktop width; only the publish boundary is a fixture, and the record it is handed is decrypted back
through the real absorb() -- which is exactly what another device does with it.

  * right-click (desktop) / long-press (phone) → Archive → the row leaves the list and "Archived (1)"
    appears; that view lists it and comes back out;
  * the record is ONE encrypted document per conversation, and a device that only receives it hides
    the same conversation;
  * a message newer than the archive brings the conversation back with nothing published;
  * Unarchive from the conversation's own header puts it back everywhere;
  * a refused publish changes nothing on screen; a search still finds an archived conversation.
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_sms_image_paste_full_app import open_texts


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


PLAIN = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"

SEED = r"""
(()=>{
  const S=PCSms._state(), now=Date.now();
  S.msgs.clear();
  const add=(doc,addr,body,ago,incoming)=>S.msgs.set(doc,{doc,address:addr,body,date:now-ago,incoming,parts:[],_at:1});
  add('arc-a1','+15550100','old chat from the dentist',86400000*30,true);
  add('arc-a2','+15550100','see you then',86400000*29,false);
  add('arc-b1','+15550199','dinner tonight?',60000,true);
  S.open='';S.q='';S.archived.clear();S.showArchived=false;
  window.published=[];window.publishOk=true;
  __PC.publish=async(kind,content,tags)=>{
    const ev={kind,content,tags,created_at:Math.floor(Date.now()/1000)+published.length,pubkey:__PC.ME.pubkey,id:'x'+published.length};
    published.push(ev);
    return publishOk?{ok:true,ev}:{ok:false};
  };
  PCSms.refreshNames();
})()
"""

ROWS = "[...document.querySelectorAll('.sms-thread')].map(b=>b.querySelector('.sms-snip').textContent)"
BOX = r"""(sel=>{const e=document.querySelector(sel);if(!e)return null;const r=e.getBoundingClientRect();
  return {l:r.left,r:r.right,t:r.top,b:r.bottom,w:r.width,vw:innerWidth,vh:innerHeight}})"""


async def _list(b, phone):
    w, h = (390, 844) if phone else (1280, 900)
    await b.call('Emulation.setDeviceMetricsOverride', {'width': w, 'height': h, 'deviceScaleFactor': 1, 'mobile': phone})
    if phone:
        await b.call('Emulation.setTouchEmulationEnabled', {'enabled': True, 'maxTouchPoints': 5})
    await open_texts(b)
    await b.js(SEED)
    await b.until("document.querySelectorAll('.sms-thread').length===2")


async def _open_row_menu(b, phone, snippet):
    sel = f"[...document.querySelectorAll('.sms-thread')].find(b=>b.textContent.includes({json.dumps(snippet)}))"
    if phone:
        await b.js(f"(()=>{{const b={sel};const r=b.getBoundingClientRect();"
                   "b.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerType:'touch',clientX:r.left+20,clientY:r.top+20}))})()")
    else:
        await b.js(f"{sel}.dispatchEvent(new MouseEvent('contextmenu',{{bubbles:true,cancelable:true}}))")
    await b.until("!!document.querySelector('.menu-pop button[data-m]')")
    await asyncio.sleep(.35)                       # a phone's menu is a sheet that slides up


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('phone', [True, False])
def test_archive_from_the_list_hides_it_everywhere_and_a_new_message_brings_it_back(phone):
    got = {}

    async def check(b):
        await _list(b, phone)
        assert await b.js(ROWS) == ['dinner tonight?', 'see you then']
        await _open_row_menu(b, phone, 'see you then')
        menu = await b.js("[...document.querySelectorAll('.menu-pop button[data-m]')].map(b=>b.textContent)")
        assert menu == ['Archive conversation'], menu
        pop = await b.js(BOX + "('.menu-pop')")
        assert pop['l'] >= 0 and pop['r'] <= pop['vw'] + .5 and pop['b'] <= pop['vh'] + .5, pop
        await b.js("document.querySelector('.menu-pop button[data-m]').click()")
        await b.until("document.querySelectorAll('.sms-thread').length===1")
        assert await b.js(ROWS) == ['dinner tonight?']
        assert await b.js("toasts.some(t=>/Archived on all your devices/.test(t))")

        # One record for the conversation: its own address, both index tags, and the number only
        # inside the ciphertext.
        ev = await b.js("published[0]")
        d = next(t[1] for t in ev['tags'] if t[0] == 'd')
        assert d.startswith('pcai:smsarc:') and '5550100' not in d, d
        assert ['l', 'pcai-sms'] in ev['tags'] and ['l', 'pcai-smsarc'] in ev['tags'], ev['tags']
        assert '5550100' not in ev['content']
        rec = await b.js(f"__PC.nip44dec(__PC.ME.pubkey,{json.dumps(ev['content'])}).then(JSON.parse)")
        assert rec['k'] == await b.js("PCSms._key('+15550100')") and rec['upto'] > 0, rec

        # The Archived view: reachable, on screen, lists it, and comes back out.
        btn = await b.js(BOX + "('#sms-arc-open')")
        assert btn and btn['l'] >= 0 and btn['r'] <= btn['vw'] + .5, btn
        assert await b.js("document.querySelector('#sms-arc-open').textContent.trim()") == 'Archived (1)'
        await b.js("document.querySelector('#sms-arc-open').click()")
        await b.until("!!document.querySelector('#sms-arc-back')")
        assert await b.js(ROWS) == ['see you then']
        await b.js("document.querySelector('#sms-arc-back').click()")
        await b.until("!document.querySelector('#sms-arc-back')")
        assert await b.js(ROWS) == ['dinner tonight?']

        # Another device: it never archived anything, it only receives the record.
        await b.js("PCSms._state().archived.clear();PCSms.refreshNames()")
        await b.until("document.querySelectorAll('.sms-thread').length===2")
        await b.js("PCSms._absorb([published[0]]).then(()=>PCSms.refreshNames())")
        await b.until("document.querySelectorAll('.sms-thread').length===1")
        assert await b.js(ROWS) == ['dinner tonight?']

        # A new text in that conversation: back in the list, on its own, nothing published.
        n = await b.js("published.length")
        await b.js("PCSms._state().msgs.set('arc-a3',{doc:'arc-a3',address:'+15550100',body:'your appointment is tomorrow',date:Date.now(),incoming:true,parts:[],_at:2});PCSms.refreshNames()")
        await b.until("document.querySelectorAll('.sms-thread').length===2")
        got['rows'] = await b.js(ROWS)
        got['arc'] = await b.js("!!document.querySelector('#sms-arc-open')")
        got['published'] = await b.js("published.length") - n
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
    assert got['rows'] == ['your appointment is tomorrow', 'dinner tonight?'], got
    assert got['arc'] is False and got['published'] == 0, got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('phone', [True, False])
def test_the_conversation_header_archives_and_unarchives(phone):
    async def check(b):
        await _list(b, phone)
        await b.js("[...document.querySelectorAll('.sms-thread')].find(b=>b.textContent.includes('see you then')).click()")
        await b.until("!!document.querySelector('#sms-archive-thread')")
        # The header still fits a phone with the extra control.
        for sel in ('#sms-archive-thread', '#sms-copy-number', '#sms-call', '#sms-back'):
            r = await b.js(BOX + f"('{sel}')")
            assert r and r['l'] >= 0 and r['r'] <= r['vw'] + .5, (sel, r)
        await b.js("document.querySelector('#sms-archive-thread').click()")
        await b.until("!document.querySelector('#sms-in') && document.querySelectorAll('.sms-thread').length===1")
        assert await b.js(ROWS) == ['dinner tonight?']

        # Open it from the Archived view: its header now offers Unarchive, which puts it back.
        await b.js("document.querySelector('#sms-arc-open').click()")
        await b.until("!!document.querySelector('#sms-arc-back')")
        await b.js("document.querySelector('.sms-thread').click()")
        await b.until("!!document.querySelector('#sms-archive-thread')")
        assert await b.js("document.querySelector('#sms-archive-thread').getAttribute('aria-label')") == 'Unarchive this conversation'
        await b.js("document.querySelector('#sms-archive-thread').click()")
        await b.until("published.length===2")
        await b.js("document.querySelector('#sms-back').click()")
        await b.until("document.querySelectorAll('.sms-thread').length===2")
        assert await b.js("published[1].content") == '', 'an unarchive is an empty record'
        assert not await b.js("!!document.querySelector('#sms-arc-open')")

        # The other device, holding the archive, receives the unarchive and shows it again.
        await b.js("PCSms._state().archived.clear();PCSms._absorb([published[0]]).then(()=>PCSms.refreshNames())")
        await b.until("document.querySelectorAll('.sms-thread').length===1")
        await b.js("PCSms._absorb([published[1]]).then(()=>PCSms.refreshNames())")
        await b.until("document.querySelectorAll('.sms-thread').length===2")
        # ...and an OLDER copy of the archive arriving late does not undo it.
        await b.js("PCSms._absorb([published[0]]).then(()=>PCSms.refreshNames())")
        await asyncio.sleep(.3)
        assert await b.js("document.querySelectorAll('.sms-thread').length") == 2
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_refused_publish_changes_nothing_and_search_still_finds_archived():
    async def check(b):
        await _list(b, False)
        await b.js("publishOk=false")
        await _open_row_menu(b, False, 'see you then')
        await b.js("document.querySelector('.menu-pop button[data-m]').click()")
        await b.until("toasts.some(t=>/could not archive/.test(t))")
        assert await b.js("document.querySelectorAll('.sms-thread').length") == 2
        assert await b.js("PCSms._state().archived.size") == 0

        await b.js("publishOk=true")
        await _open_row_menu(b, False, 'see you then')
        await b.js("document.querySelector('.menu-pop button[data-m]').click()")
        await b.until("document.querySelectorAll('.sms-thread').length===1")
        await b.js("const q=document.querySelector('#sms-q');q.value='dentist';q.dispatchEvent(new Event('input'))")
        await b.until("document.querySelectorAll('.sms-thread').length===1 && document.querySelector('.sms-thread').textContent.includes('see you then')")
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_slow_relay_says_archiving_at_once_and_a_second_tap_sends_nothing_more():
    """'i tried to archive that one then nothing happened' -- then it was gone: the record was waiting
    on a slow relay, and the screen said nothing until it answered."""
    async def check(b):
        await _list(b, False)
        await b.js("window.release=null;const fast=__PC.publish;"
                   "__PC.publish=(...a)=>new Promise(r=>{window.release=()=>fast(...a).then(r);})")
        for _ in range(2):
            await _open_row_menu(b, False, 'see you then')
            await b.js("document.querySelector('.menu-pop button[data-m]').click()")
            await b.until("toasts.some(t=>/^Archiving/.test(t))")
        assert await b.js("document.querySelectorAll('.sms-thread').length") == 2, \
            'nothing is hidden before the relay accepts it'
        await b.js("release()")
        await b.until("document.querySelectorAll('.sms-thread').length===1")
        assert await b.js("published.length") == 1, 'the second tap was the same request, not another'
    asyncio.run(desktop.with_browser('online', '', check, PLAIN))
