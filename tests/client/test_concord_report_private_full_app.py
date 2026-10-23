"""REPORTING A CONCORD MESSAGE GOES TO THE ROOM'S MODERATORS, PRIVATELY -- NEVER A PUBLIC NIP-56.

"add report to ... concord too". A Concord room is PRIVATE and encrypted: a public kind 1984 pointing at
(or quoting) a decrypted message would publish both the content and who is in the room. So the 🚩 on a
message opens the SAME report sheet as a post, but the report is delivered as NIP-17 gift-wrapped DMs to
the room's owner and each moderator (the people CORD's control plane says hold MANAGE_MESSAGES -- they can
already read the room), never to the reported member and never to the reporter themself. This node's
admins are told ONLY when the person ticks "also tell this server's admins", which is offered only when
the room lives on this node's relay and the node names admins.

Real bundled client (concord.js, menus.js, dms.js, the real NIP-17 signer); only the relay socket and
the CORD reader are fixtures -- the reader stands in for the room's decrypted control plane and history.
"""
import asyncio
import base64
import json
import os
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop

SHOTS = Path(os.environ.get('PC_REPORT_SHOTS', '/tmp/pc-report-shots'))


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ROOM = r'''(()=>{
  const pk=n=>NostrTools.getPublicKey(new Uint8Array(32).fill(n));
  window.__bob=pk(2); window.__owner=pk(3); window.__mod=pk(4); window.__admin=pk(5);
  const room={name:'Secret club',communityId:'c'.repeat(64),naddr:'fixture-community',
    channels:[{id:'fixture-general',name:'general'}],
    cord:{bundle:{owner:__owner,relays:['wss://fixture.invalid'],epoch:1},hydrated:true}};
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
  window.PosterCordReader={
    inspectControl:()=>({controlPubkeys:[],moderators:[__mod,__bob],channels:[{id:'fixture-general',name:'general',streamPubkeys:[]}]}),
    inspectChat:async()=>({messages:[
      {id:'msg-from-bob',pubkey:__bob,text:'PRIVATE-ROOM-SECRET-TEXT',at:Date.now()-60000,kind:9,tags:[]},
      {id:'msg-mine',pubkey:__PC.me().pubkey,text:'my own words',at:Date.now()-30000,kind:9,tags:[]}],
      reactions:[],reactionIds:[]})};
  window.__publishOK=true;
  __PC.switchMessagesTab('concord');
})()'''

EXTRA = "localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"


async def _in_room(b, width):
    await b.call('Emulation.setDeviceMetricsOverride', {'width': width, 'height': 820, 'deviceScaleFactor': 1, 'mobile': width < 600})
    await desktop.login(b)
    await b.js(ROOM)
    await b.until("!!document.querySelector('#cc-input')")
    await b.js("""(()=>{const app=document.querySelector('.cc-app');if(app&&!app.classList.contains('show-chat')){const ch=document.querySelector('.cc-channel');if(ch)ch.click();}})()""")
    await b.until("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('PRIVATE-ROOM-SECRET-TEXT'))")


def _msg(text):
    return "[...document.querySelectorAll('.cc-message')].find(x=>x.textContent.includes(%s))" % json.dumps(text)


async def _open_report(b):
    await b.js(f"""(()=>{{const m={_msg('PRIVATE-ROOM-SECRET-TEXT')};m.dispatchEvent(new MouseEvent('mouseover',{{bubbles:true}}));
        const t=m.querySelector('[data-cc-actions]');if(t)t.click();m.querySelector('[data-cc-report]').click();}})()""")
    await b.until("!!document.querySelector('.rep-form')")


async def _submit(b, admins=False):
    await b.js("""(()=>{document.querySelector('.rep-form input[value=spam]').click();
        document.querySelector('#rep-comment').value='COMMENT-ABOUT-BOB';
        const a=document.querySelector('#rep-admins'); if(a&&%s)a.click();
        document.querySelector('#rep-submit').click()})()""" % ('true' if admins else 'false'))
    await b.until("!!document.querySelector('.rep-done')")
    await asyncio.sleep(1.0)


async def _wire(b):
    return await b.js("""(()=>{const me=__PC.me().pubkey;return {
        reports:__published.filter(e=>e.kind===1984).length,
        to:[...new Set(__published.filter(e=>e.kind===1059).map(e=>(e.tags.find(t=>t[0]==='p')||[])[1]).filter(p=>p&&p!==me))].sort(),
        leaked:__published.filter(e=>JSON.stringify(e).includes('PRIVATE-ROOM-SECRET-TEXT')||JSON.stringify(e).includes('COMMENT-ABOUT-BOB')).map(e=>e.kind),
        done:document.querySelector('.rep-done').textContent}})()""")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
@pytest.mark.parametrize('width', [360, 1280])
def test_a_concord_report_goes_privately_to_the_rooms_moderators(width):
    got = {}

    async def check(b):
        await _in_room(b, width)
        # Not on your own message.
        assert not await b.js(f"!!{_msg('my own words')}.querySelector('[data-cc-report]')"), "a report button on my own message"
        await _open_report(b)
        text = await b.js("document.querySelector('.rep-form').closest('.modal').textContent")
        assert 'moderators' in text.lower() and 'not public' in text.lower(), text
        assert not await b.js("!!document.querySelector('#rep-admins')"), "the admin option is offered on a node that names no admins"
        geo = await b.js("""(()=>{const m=document.querySelector('.rep-form').closest('.modal').getBoundingClientRect(),s=document.querySelector('#rep-submit').getBoundingClientRect();
            return {l:m.left,r:m.right,sb:s.bottom,vh:innerHeight,vw:document.documentElement.clientWidth,page:document.documentElement.scrollWidth}})()""")
        assert geo['l'] >= 0 and geo['r'] <= geo['vw'] + .5 and geo['page'] <= geo['vw'] and geo['sb'] <= geo['vh'], geo
        if width == 360:
            SHOTS.mkdir(parents=True, exist_ok=True)
            (SHOTS / 'concord-report-sheet-360.png').write_bytes(
                base64.b64decode((await b.call('Page.captureScreenshot', {'format': 'png'}))['data']))
        await _submit(b)
        got.update(await _wire(b))
        got['expect'] = sorted([await b.js('__owner'), await b.js('__mod')])
        # What the MODERATOR reads: unwrap the gift wrap addressed to them with THEIR key.
        got['opened'] = await b.js(r'''(()=>{const sk=new Uint8Array(32).fill(4),N=NostrTools.nip44;
            const w=__published.find(e=>e.kind===1059&&e.tags.some(t=>t[0]==='p'&&t[1]===__mod));if(!w)return null;
            const ck=(pk)=>(N.getConversationKey||N.v2.utils.getConversationKey)(sk,pk),dec=(c,k)=>(N.decrypt||N.v2.decrypt)(c,k);
            const seal=JSON.parse(dec(w.content,ck(w.pubkey))),rumor=JSON.parse(dec(seal.content,ck(seal.pubkey)));
            return {sealBy:seal.pubkey,kind:rumor.kind,text:rumor.content};})()''')
        got['me'] = await b.js('__PC.me().pubkey')
        if width == 1280:
            # The report as a DM thread reads it: a button that opens the room AT the message.
            await b.js("__PC.messageUser(__mod)")
            await b.until("!!document.querySelector('.dm-cc-open')")
            got['dm_text'] = await b.js("document.querySelector('.dm-cc-open').closest('.bubble,.dm-msg,[data-mid],div').textContent")
            await b.js("document.querySelector('.dm-cc-open').click()")
            await b.until("__PC.isView('concord') && !!document.querySelector('.cc-message[data-message-id=\"msg-from-bob\"]')")
            got['jumped'] = True
        if width == 360:
            (SHOTS / 'concord-report-done-360.png').write_bytes(
                base64.b64decode((await b.call('Page.captureScreenshot', {'format': 'png'}))['data']))
    asyncio.run(desktop.with_browser('online', '', check, EXTRA))
    assert got['reports'] == 0, ('a PUBLIC kind 1984 was published about a private room', got)
    assert got['to'] == got['expect'], ('the report did not go to exactly the owner and the moderators '
                                         '(never the reported member, never this node\'s admins unasked)', got)
    assert got['leaked'] == [], ('the message text or the comment appeared in a published event in the clear', got)
    assert "moderators" in got['done'].lower(), got
    if width == 1280:
        assert got.get('jumped'), got
        assert 'Open: concord:' not in got['dm_text'], got['dm_text']
    o = got['opened']
    assert o and o['kind'] == 14 and o['sealBy'] == got['me'], ('the moderator could not open a NIP-17 DM from the reporter', o)
    for want in ('Concord report', 'Secret club', '#general', 'Reported member:', 'Reason: Spam',
                 'Comment: COMMENT-ABOUT-BOB', 'PRIVATE-ROOM-SECRET-TEXT', 'Open: concord:'):
        assert want in o['text'], (want, o['text'])


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_this_nodes_admins_are_told_only_when_asked():
    got = {}

    async def check(b):
        await _in_room(b, 1280)
        await b.js("(()=>{const pk=NostrTools.getPublicKey(new Uint8Array(32).fill(5));__PC.clientConfig().admin_npubs=[NostrTools.nip19.npubEncode(pk)];})()")
        await _open_report(b)
        assert await b.js("!!document.querySelector('#rep-admins') && !document.querySelector('#rep-admins').checked"), \
            "the admin option must be offered, and OFF, when the room is on this node and it names admins"
        await _submit(b, admins=True)
        got.update(await _wire(b))
        got['expect'] = sorted([await b.js('__owner'), await b.js('__mod'), await b.js('__admin')])
    asyncio.run(desktop.with_browser('online', '', check, EXTRA))
    assert got['reports'] == 0, got
    assert got['to'] == got['expect'], got
    assert got['leaked'] == [], got


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_after_reporting_a_member_the_rooms_owner_can_ban_them_from_the_sheet():
    """The report sheet's "🚫 Ban from this community" (menus.js `#rep-ban`, `opts.ban`) existed and was never offered:
    Concord passed no `ban`, though the sheet's own note tells a moderator to "ban the member yourself" (2026-10-22).
    Now the room's owner gets it after the report goes out, and it asks to ban exactly the reported member -- the same
    ban the member menu performs. An ordinary member is never offered it."""
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', {'width': 1280, 'height': 820, 'deviceScaleFactor': 1, 'mobile': False})
        await desktop.login(b)
        # The viewer OWNS the room this time; the report goes to the other moderator.
        await b.js(ROOM.replace("owner:__owner,", "owner:__PC.me().pubkey,"))
        await b.until("!!document.querySelector('#cc-input')")
        await b.until("[...document.querySelectorAll('.cc-message')].some(m=>m.textContent.includes('PRIVATE-ROOM-SECRET-TEXT'))")
        await _open_report(b)
        await _submit(b)
        await b.until("!!document.querySelector('#rep-ban')")
        await b.js("window.__bansBefore=__published.length; document.querySelector('#rep-ban').click(); 1")
        await b.until("!!document.querySelector('.uiconfirm-btns [data-uc=\"1\"]')")
        ask = await b.js("document.querySelector('.uiconfirm-msg').textContent")
        assert 'from this community' in ask and 'Ban' in ask, ask
        bob = await b.js("NostrTools.nip19.npubEncode(__bob).slice(0,12)")
        assert bob in ask or 'npub' in ask, ('the ban does not name the reported member', ask)
        # Cancel: nothing is published.
        await b.js("document.querySelector('.uiconfirm-btns [data-uc]:not([data-uc=\"1\"])').click(); 1")
        await asyncio.sleep(.5)
        assert await b.js("__published.length") == await b.js("__bansBefore"), 'cancelling the ban published something'
        assert not await b.js("__errors"), await b.js("__errors")

    asyncio.run(desktop.with_browser('online', '', check, EXTRA))


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_ordinary_member_is_not_offered_ban_after_reporting():
    async def check(b):
        await _in_room(b, 1280)
        await _open_report(b)
        await _submit(b)
        assert not await b.js("!!document.querySelector('#rep-ban')"), 'a member who cannot ban was offered Ban'

    asyncio.run(desktop.with_browser('online', '', check, EXTRA))
