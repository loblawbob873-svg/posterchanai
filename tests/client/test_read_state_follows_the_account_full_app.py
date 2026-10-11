"""WHAT YOU HAVE SEEN ON ONE DEVICE IS SEEN ON ALL OF THEM.

Reported: "Look at notifications and DMs. The blue popup count on those sidebars goes away. But then I look
at poster.place on mobile and all those same notifications and DMs claim to still be new until I look at
them again." The read markers were localStorage (`pc_notif_seen`, ClientSettings `dmSeen`), one browser
profile each, so every device had to be told separately.

Driven against the SHIPPED bundle. Device A reads its notifications and its messages; the account's
`pcai:seen` document (kind 30078, NIP-44 to the user's own key) reaches the relay. Device B is the same
account with NOTHING local -- localStorage, sessionStorage and the IndexedDB cache wiped, only the relay
kept -- and must show neither badge for what A read, while something NEWER still lights both.
Only the relay socket is a fixture (test_dm_newest_first_full_app.RELAY).
"""
import asyncio
from pathlib import Path

import pytest

from tests.client import test_desktop_offline_full_app as desktop
from tests.client.test_dm_newest_first_full_app import RELAY


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


# A DM (real NIP-17 wrap) from `peer` and a mention from `who`, `ago` seconds before now.
SEED = r'''((ago,dms,mentions)=>{
 const me=new Uint8Array(32).fill(1),mePk=NostrTools.getPublicKey(me),now=Math.floor(Date.now()/1000);
 const {createRumor,createSeal}=NostrTools.nip59;const out=[];
 for(let i=0;i<dms;i++){const sk=new Uint8Array(32).fill(99),t=now-ago+i;
  const seal=createSeal(createRumor({kind:14,created_at:t,tags:[['p',mePk]],content:'hello '+ago+' '+i},sk),sk,mePk);
  const eph=NostrTools.generateSecretKey();
  out.push(NostrTools.finalizeEvent({kind:1059,created_at:t-Math.floor(Math.random()*2*86400),tags:[['p',mePk]],
   content:NostrTools.nip44.encrypt(JSON.stringify(seal),NostrTools.nip44.getConversationKey(eph,mePk))},eph));}
 for(let i=0;i<mentions;i++)out.push(NostrTools.finalizeEvent({kind:1,created_at:now-ago+i,tags:[['p',mePk]],
   content:'hey you '+ago+' '+i},new Uint8Array(32).fill(50+i)));
 const r=_rel();for(const e of out)r.push(e);localStorage.setItem('__relayEvents',JSON.stringify(r));return out.length;})'''

BELL = ("[...document.querySelectorAll('#notif-badge,#notif-badge-m,#rb-notif-badge')]"
        ".filter(b=>!b.classList.contains('hidden')).map(b=>b.textContent)")
MSGS = ("[...document.querySelectorAll('#dm-badge,#dm-badge-m')]"
        ".filter(b=>!b.classList.contains('hidden')).map(b=>b.textContent)")

# The newest pcai:seen this account published, opened with its own key (null if none yet).
SEEN_DOC = r'''(()=>{const me=new Uint8Array(32).fill(1),mePk=NostrTools.getPublicKey(me);
 const evs=_rel().filter(e=>e.kind===30078&&e.pubkey===mePk&&e.tags.some(t=>t[0]==='d'&&t[1]==='pcai:seen'))
   .sort((a,b)=>b.created_at-a.created_at);
 if(!evs.length)return null;const ev=evs[0];
 return {ev,doc:JSON.parse(NostrTools.nip44.decrypt(ev.content,NostrTools.nip44.getConversationKey(me,mePk)))};})()'''

# A NEW DEVICE: nothing local survives except the relay fixture's own store.
NEW_DEVICE = r'''(async()=>{const rel=localStorage.getItem('__relayEvents');localStorage.clear();sessionStorage.clear();
 localStorage.setItem('__relayEvents',rel);
 await new Promise(r=>{const q=indexedDB.deleteDatabase('posterchan-nostr');q.onsuccess=q.onerror=q.onblocked=()=>r();});
 return true;})()'''


async def until(b, expression, seconds=15):
    for _ in range(int(seconds * 10)):
        if await b.js(expression):
            return
        await asyncio.sleep(.1)
    raise AssertionError({'waiting': expression, 'bell': await b.js(BELL), 'msgs': await b.js(MSGS),
                          'unread': await b.js('__PC.notifUnread()'), 'errors': await b.js('__errors'),
                          'local': await b.js("Object.keys(localStorage).filter(k=>k.startsWith('pc_seen:')).map(k=>localStorage.getItem(k))"),
                          'published': await b.js("(window.__published||[]).map(e=>e.kind+':'+((e.tags.find(t=>t[0]==='d')||[])[1]||''))")})


async def boot(b, fresh_login):
    if fresh_login:
        await desktop.login(b)
    else:
        await b.until('!!window.__PC && !!__PC.me()')
    await b.js("try{ if(window.PCOS && PCOS.isOn()) PCOS.exit(); }catch(_){}")


@pytest.mark.skipif(not Path('/opt/google/chrome/chrome').exists(), reason='Chrome required')
def test_read_on_one_device_is_read_on_the_next_and_newer_still_counts():
    async def check(b):
        await b.call('Emulation.setDeviceMetricsOverride', dict(width=1280, height=900, deviceScaleFactor=1, mobile=False))
        await b.until("document.body.classList.contains('guest')")
        await b.js(SEED + "(600,2,1)")                              # 2 DMs and a mention, ten minutes old
        # ---- device A: sees them, then reads them ----
        await boot(b, True)
        await until(b, "__PC.dmStats().done>=2")
        await until(b, MSGS + ".length>0 && __PC.notifUnread()>=1")
        await b.js("__PC.switchView('notifications')")
        await asyncio.sleep(.6)
        await b.js("__PC.switchView('messages')")
        await asyncio.sleep(.6)
        await b.js("__PC.switchView('home')")
        await until(b, BELL + ".length===0 && " + MSGS + ".length===0")
        # Leaving the page is when a phone kills it: the debounced write must go out then.
        await b.js("window.dispatchEvent(new Event('pagehide'))")
        me = await b.js("NostrTools.getPublicKey(new Uint8Array(32).fill(1))")
        peer = await b.js("NostrTools.getPublicKey(new Uint8Array(32).fill(99))")
        # (the seed was ten minutes before it ran; 900s leaves the test its own run time)
        await until(b, "(d=>!!d && d.doc.notif>=Math.floor(Date.now()/1000)-900 && "
                       "Math.max(d.doc.dmAll||0,(d.doc.dm||{})[" + repr(peer) + "]||0)>=Math.floor(Date.now()/1000)-900)(" + SEEN_DOC + ")")
        got = await b.js(SEEN_DOC)
        ev = got['ev']
        # Sealed to the user: the relay learns neither whom they talk to nor when they read.
        assert ['d', 'pcai:seen'] in ev['tags'], ev['tags']
        assert {t[0] for t in ev['tags']} <= {'d', 'client'}, ('a tag names somebody', ev['tags'])
        for leak in (peer, me, 'notif', 'dmAll'):
            assert leak not in ev['content'], ('plaintext in the seen document', leak)
        assert got['doc']['v'] == 1, got['doc']

        # ---- device B: same account, nothing local ----
        assert await b.js(NEW_DEVICE)
        await b.call('Page.reload')
        await asyncio.sleep(.3)
        await b.until("!!window.__PC && document.body.classList.contains('guest')")
        await boot(b, True)
        await until(b, "__PC.dmStats().done>=2 && __PC.notifItems(30).length>=1")
        await until(b, BELL + ".length===0 && " + MSGS + ".length===0 && __PC.notifUnread()===0")
        await asyncio.sleep(1.5)                                     # and it stays that way
        assert await b.js(BELL) == [] and await b.js(MSGS) == [], (await b.js(BELL), await b.js(MSGS))

        # ---- something NEWER, on device B: unread, on both badges ----
        await b.js(SEED + "(5,1,1)")
        await b.call('Page.reload')
        await asyncio.sleep(.3)
        await boot(b, False)
        await until(b, "__PC.dmStats().done>=3")
        await until(b, "(m=>m.length>0 && m.every(t=>t==='1'))(" + MSGS + ") && __PC.notifUnread()===1")
        assert not await b.js('__errors'), await b.js('__errors')

    asyncio.run(desktop.with_browser('online', '', check, RELAY))
