"""ONE ACCOUNT'S COMMUNITIES ARE NEVER DRAWN FOR ANOTHER, END TO END.

Reported: "The app isn't properly separating the group memberships between both accounts. Using
Concord with @beatboxserenade shows groups that profile is not in... the groups are those from [the
other] profile." Concord kept its joined-room list in ONE device-wide localStorage key
(`pc.concord.invites`), and the account switcher reloads the page without touching it -- so the
account switched TO was drawn, swept and backfilled with the account switched FROM's rooms.

Both tests drive the shipped bundle in Chrome: two real keys, each with its own signed, NIP-44-sealed
membership vault served by the fixture relay, and the app's own account switch
(`__PC.accountAct(pubkey)` -> Session.save -> reload, the path the Accounts menu takes). Only the relay
socket and the CORD reader are fixtures; the membership read, decryption and room list are the app's.

1. The switch: A's room is on A's screen; after switching, B's screen shows B's room and none of A's;
   switching back shows A's again and none of B's.
2. The migration: a device that used BOTH accounts before this fix holds one shared list with rooms of
   each. Each account is handed only the rooms its own vault names, the old list is left byte for
   byte, and a room nothing attributes (no vault entry) is drawn for nobody until the person claims
   it in "Check my communities".
"""
import asyncio
import json
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


ALPHA = 'a1' * 32
BRAVO = 'b2' * 32
CHARLIE = 'c3' * 32

# Survives every reload: the relay's events live in localStorage, and the fixture socket answers EOSE
# so a membership read completes instead of waiting out its timeout.
EXTRA = r'''
localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));
window.__events=JSON.parse(localStorage.getItem('__fixtureEvents')||'[]');
{const send=WebSocket.prototype.send;WebSocket.prototype.send=function(raw){send.call(this,raw);try{const m=JSON.parse(raw);
  if(Array.isArray(m)&&m[0]==='REQ')setTimeout(()=>this.fire('message',['EOSE',m[1]]),60);}catch(_){}};}
'''

# Two vaults, each sealed to its own key, naming one community each.
VAULTS = r'''(()=>{
  const T=NostrTools,n=T.nip44,conv=n.getConversationKey||(n.v2&&n.v2.utils&&n.v2.utils.getConversationKey),
        enc=n.encrypt||(n.v2&&n.v2.encrypt);
  const vault=(fill,rooms)=>{const sk=new Uint8Array(32).fill(fill),pk=T.getPublicKey(sk);
    const doc={entries:rooms.map(([cid,name])=>({community_id:cid,added_at:1000,invite_ref:'',
      current:{name,community_id:cid,channels:[],relays:['wss://fixture.invalid']}})),tombstones:[]};
    return T.finalizeEvent({kind:13302,created_at:Math.floor(Date.now()/1000)-60,tags:[],content:enc(JSON.stringify(doc),conv(sk,pk))},sk);};
  const events=[vault(1,[['ALPHA','Alpha room']]),vault(2,[['BRAVO','Bravo room']])];
  localStorage.setItem('__fixtureEvents',JSON.stringify(events));window.__events=events;
  return events.map(e=>e.pubkey);
})()'''.replace('ALPHA', ALPHA).replace('BRAVO', BRAVO)

READER = r'''(()=>{window.PosterCordReader={inspectControl:()=>({controlPubkeys:[],channels:[{id:'general-id',name:'general',streamPubkeys:[]}]}),
  inspectChat:async()=>({messages:[],reactions:[],reactionIds:[]})};return true;})()'''

ON_SCREEN = "(()=>{const f=document.querySelector('#feed');return f?f.innerHTML:'';})()"


async def login(b, fill):
    await b.until("document.body.classList.contains('guest')")
    await b.js("(()=>{const key=new Uint8Array(32).fill(%d);document.querySelector('#nsec-input').value=NostrTools.nip19.nsecEncode(key);document.querySelector('#btn-nsec-login').click()})()" % fill)
    await b.until('!!__PC.me()')


async def wait(b, expression, seconds=40):
    for _ in range(int(seconds * 10)):
        if await b.js(expression):
            return True
        await asyncio.sleep(.1)
    return False


async def open_concord(b):
    await b.js(READER)
    await b.js("__PC.switchMessagesTab('concord')")
    await b.until("document.body.classList.contains('concord-view')")


async def switch_to(b, pubkey):
    await b.js("window.__beforeSwitch=1")
    await b.js("__PC.accountAct(%s)" % json.dumps(pubkey))
    await b.until("!window.__beforeSwitch && !!window.__PC && !!window.NostrTools")
    await b.until("!!__PC.me() && __PC.me().pubkey===%s" % json.dumps(pubkey))


async def two_accounts(b):
    """B signs in first (remembered), then 'Add an account' and A signs in: both are in the switcher."""
    await login(b, 2)
    await b.js("window.__beforeAdd=1")
    await b.js("__PC.accountAct('add')")
    await b.until("!window.__beforeAdd && !!window.__PC && !!window.NostrTools")
    await login(b, 1)
    return await b.js(VAULTS)


def shows(html, name):
    return name in html


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_switching_accounts_never_shows_the_other_accounts_communities():
    got = {}

    async def check(b):
        a_pk, b_pk = await two_accounts(b)
        got['pks'] = (a_pk, b_pk)
        await open_concord(b)
        got['a_has_alpha'] = await wait(b, ON_SCREEN + ".includes('Alpha room')")
        got['a_first'] = await b.js(ON_SCREEN)

        await switch_to(b, b_pk)
        await open_concord(b)
        got['b_has_bravo'] = await wait(b, ON_SCREEN + ".includes('Bravo room')")
        await asyncio.sleep(1.5)   # every membership pass B will run has run; nothing may add A's room
        got['b_screen'] = await b.js(ON_SCREEN)

        await switch_to(b, a_pk)
        await open_concord(b)
        got['a_back_has_alpha'] = await wait(b, ON_SCREEN + ".includes('Alpha room')")
        await asyncio.sleep(1.5)
        got['a_back_screen'] = await b.js(ON_SCREEN)

    asyncio.run(desktop.with_browser('online', '', check, EXTRA))
    assert got['a_has_alpha'], ("account A's own community never appeared", got.get('a_first', '')[-1500:])
    assert got['b_has_bravo'], ("account B's own community never appeared", got['b_screen'][-1500:])
    assert not shows(got['b_screen'], 'Alpha room'), "account B was shown account A's community"
    assert got['a_back_has_alpha'], "switching back, account A's community was gone"
    assert not shows(got['a_back_screen'], 'Bravo room'), "account A was shown account B's community"


LEGACY = r'''(()=>{
  const room=(cid,name,marker)=>({name,communityId:cid,naddr:'community-'+cid,url:'',local:false,legacyMarker:marker,
    channels:[{name:'general',private:false,id:'general-id'}],cord:{bundle:{community_id:cid,name,channels:[],relays:['wss://fixture.invalid']},hydrated:true}});
  const rooms=[room('ALPHA','Alpha room','kept-alpha'),room('BRAVO','Bravo room','kept-bravo'),room('CHARLIE','Charlie room','kept-charlie')];
  localStorage.setItem('pc.concord.invites',JSON.stringify(rooms));
  window.__legacyBefore=localStorage.getItem('pc.concord.invites');
  localStorage.setItem('__legacyBefore',window.__legacyBefore);
  return true;
})()'''.replace('ALPHA', ALPHA).replace('BRAVO', BRAVO).replace('CHARLIE', CHARLIE)

MY_ROOMS = "(()=>{const pk=__PC.me().pubkey;try{return JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+pk)||'[]').map(r=>[r.name,r.legacyMarker||''])}catch(_){return null}})()"


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_a_shared_pre_separation_list_is_handed_out_by_each_accounts_own_evidence():
    got = {}

    async def check(b):
        a_pk, b_pk = await two_accounts(b)
        # The device as an older build left it: one list holding rooms of BOTH accounts plus one that
        # no vault names. Written while signed in as A, as the old code wrote it.
        await b.js(LEGACY)
        await switch_to(b, b_pk)
        await open_concord(b)
        got['b_has_bravo'] = await wait(b, ON_SCREEN + ".includes('Bravo room')")
        await asyncio.sleep(1.5)
        got['b_screen'] = await b.js(ON_SCREEN)
        got['b_rooms'] = await b.js(MY_ROOMS)
        got['legacy_intact_b'] = await b.js("localStorage.getItem('pc.concord.invites')===localStorage.getItem('__legacyBefore')")
        # The room nothing attributes is offered to the person, and claiming it is the person's answer.
        await b.js("document.querySelector('#cc-discovery').click()")
        await b.until("!!document.querySelector('#cc-check-memberships')")
        got['hint'] = await b.js("(document.querySelector('.cc-legacy-hint')||{}).textContent||''")
        await b.js("document.querySelector('#cc-check-memberships').click()")
        got['claim_offered'] = await wait(b, "[...document.querySelectorAll('[data-cc-claim]')].some(x=>x.textContent.includes('Charlie room'))", 20)
        got['report'] = await b.js("(document.querySelector('#cc-mcheck-out')||{}).textContent||''")
        if got['claim_offered']:
            await b.js("[...document.querySelectorAll('[data-cc-claim]')].find(x=>x.textContent.includes('Charlie room')).click()")
            got['b_claimed'] = await wait(b, MY_ROOMS + ".some(r=>r[0]==='Charlie room')", 10)
        got['b_rooms_after_claim'] = await b.js(MY_ROOMS)

        await switch_to(b, a_pk)
        await open_concord(b)
        got['a_has_alpha'] = await wait(b, ON_SCREEN + ".includes('Alpha room')")
        await asyncio.sleep(1.5)
        got['a_screen'] = await b.js(ON_SCREEN)
        got['a_rooms'] = await b.js(MY_ROOMS)
        got['legacy_intact_a'] = await b.js("localStorage.getItem('pc.concord.invites')===localStorage.getItem('__legacyBefore')")

    asyncio.run(desktop.with_browser('online', '', check, EXTRA))
    assert got['b_has_bravo'], ("B's own room never appeared", got.get('b_screen', '')[-1500:])
    assert not shows(got['b_screen'], 'Alpha room'), "B was handed A's room from the shared list"
    assert not shows(got['b_screen'], 'Charlie room'), 'a room no evidence attributes was shown as joined'
    # Adopted WHOLE from the old list (its bundle and settings), not rebuilt from the vault snapshot.
    assert ['Bravo room', 'kept-bravo'] in (got['b_rooms'] or []), got['b_rooms']
    assert got['legacy_intact_b'] and got['legacy_intact_a'], 'the pre-separation list was rewritten'
    assert got['claim_offered'], ('the unattributed room was not offered for claiming', got['report'])
    assert got.get('b_claimed'), got['b_rooms_after_claim']
    assert got['a_has_alpha'], ("A's own room never appeared", got.get('a_screen', '')[-1500:])
    assert ['Alpha room', 'kept-alpha'] in (got['a_rooms'] or []), got['a_rooms']
    assert not shows(got['a_screen'], 'Bravo room'), "A was handed B's room"
    assert not shows(got['a_screen'], 'Charlie room'), "B's claim was handed to A as well"


UNREADABLE_VAULT = r'''(()=>{
  // B's vault exists but will not decrypt: "could not read it", never "B is in nothing".
  const T=NostrTools,sk=new Uint8Array(32).fill(2);
  const bad=T.finalizeEvent({kind:13302,created_at:Math.floor(Date.now()/1000)-30,tags:[],content:'not-a-nip44-payload'},sk);
  const keep=JSON.parse(localStorage.getItem('__fixtureEvents')||'[]').filter(e=>e.pubkey!==bad.pubkey);
  localStorage.setItem('__fixtureEvents',JSON.stringify([...keep,bad]));
  return true;
})()'''


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_an_unreadable_vault_hands_out_nothing_and_keeps_the_old_list():
    got = {}

    async def check(b):
        a_pk, b_pk = await two_accounts(b)
        good = await b.js("localStorage.getItem('__fixtureEvents')")
        await b.js(LEGACY)
        await b.js(UNREADABLE_VAULT)
        await switch_to(b, b_pk)
        await open_concord(b)
        await asyncio.sleep(4)
        got['b_screen'] = await b.js(ON_SCREEN)
        got['b_rooms'] = await b.js(MY_ROOMS)
        got['legacy_intact'] = await b.js("localStorage.getItem('pc.concord.invites')===localStorage.getItem('__legacyBefore')")
        # The vault becomes readable: the room it names is handed over then, not lost.
        await b.js("localStorage.setItem('__fixtureEvents',%s)" % json.dumps(good))
        await switch_to(b, a_pk)
        await switch_to(b, b_pk)
        await open_concord(b)
        got['b_later'] = await wait(b, ON_SCREEN + ".includes('Bravo room')")
        got['b_rooms_later'] = await b.js(MY_ROOMS)

    asyncio.run(desktop.with_browser('online', '', check, EXTRA))
    assert not shows(got['b_screen'], 'Bravo room') and not shows(got['b_screen'], 'Alpha room'), \
        'a room was handed out on a vault that could not be read'
    assert got['b_rooms'] == [], got['b_rooms']
    assert got['legacy_intact'], 'the pre-separation list was rewritten on a failed read'
    assert got['b_later'], 'once the vault could be read, the room it names never arrived'
    assert ['Bravo room', 'kept-bravo'] in (got['b_rooms_later'] or []), got['b_rooms_later']
