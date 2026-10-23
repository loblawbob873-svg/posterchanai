"""JOINING A COMMUNITY FROM DISCOVER OPENS THAT COMMUNITY -- never another one that once carried its invite.

Reported again 2026-10-22: "I tried to join the rin anime community and it loads the other community and quickly
closes", "rin anime room shows as Sorsmenedék again". Both communities were made by ONE inviter, so their invites
share an naddr and only the token tells them apart; an earlier merge left Sorsmenedék's row carrying Rin's invite in
`inviteAliases`. A listing has no decrypted bundle yet, so `sameRoom` matched it to that alias: Join opened
Sorsmenedék, then merged Rin's decrypted invite INTO it -- and the list re-merged them on every load.

Real minted communities (same inviter, invites on an outside relay), the shipped bundled client and Discover list;
the saved state is the user's: a hydrated Sorsmenedék row whose aliases include Rin's invite.
"""
import asyncio
import json

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client import test_concord_talks_only_to_this_node_full_app as T
from tests.client.test_concord_reads_this_node_full_app import CHROME, CORD_RELAYS, open_concord, wait_for


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


SETUP = r'''(async()=>{
  const sk=new Uint8Array(32).fill(7),owner=NostrTools.getPublicKey(sk),sign=async t=>NostrTools.finalizeEvent(t,sk);
  const mk=async name=>PosterCord.createCommunity({owner,name,relays:['wss://ext-a.invalid'],base:'https://armada.buzz',signEvent:sign});
  const sors=await mk('Sorsmenedek'),rin=await mk('Rin anime');
  // One inviter: both invites are the SAME replaceable address (33301, link key, d=''), so a relay holds the newest.
  window.__upHeld=[...sors.events.filter(e=>e.kind!==33301),...rin.events].flatMap(ev=>(window.__upRelays||[]).map(url=>({url,ev})));
  window.__directory=[await sign({kind:1,created_at:Math.floor(Date.now()/1000),content:'Rin anime\n\n'+rin.url,tags:[['t','concord']]})];
  window.__dirHold=false;
  // The user's saved row: Sorsmenedek, hydrated, still carrying Rin's invite from the old merge.
  const bundle=PosterCord.openInvite(sors.url,sors.events).bundle;
  const info=PosterCordReader.inspectControl(bundle,sors.events.filter(e=>e.kind===1059));
  const row={name:'Sorsmenedek',communityId:sors.communityId,url:sors.url,inviteAliases:[sors.url,rin.url],
    channels:info.channels.map(c=>({name:c.name,id:c.id,private:!!c.private})),cord:{bundle,hydrated:true}};
  const pk=__PC.me().pubkey;
  localStorage.setItem('pc.concord.rooms.v1.'+pk,JSON.stringify([row]));
  localStorage.setItem('pc.concord.active.v1.'+pk,'0');
  window.__ids={sors:sors.communityId,rin:rin.communityId,rinUrl:rin.url};
  // The invite lookup is answered at the client's relay door with what a relay holds at that shared address: the
  // NEWEST invite -- Rin's (one inviter: both invites are the same replaceable 33301 address).
  {const held=[...sors.events.filter(e=>e.kind!==33301),...rin.events],
     hit=(f,e)=>(!f.kinds||f.kinds.includes(e.kind))&&(!f.authors||f.authors.includes(e.pubkey))&&(!f.ids||f.ids.includes(e.id));
   const answer=f=>Promise.resolve([...held,...window.__directory].filter(e=>(f||[]).some(x=>hit(x,e))));
   __PC.relayQueryFrom=(r,f,o)=>answer(f);__PC.relayQuery=(f)=>answer(f);}
  PCConcord.render();return window.__ids;
})()'''

ROOMS = "JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]')"


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_joining_rin_from_discover_opens_rin_and_frees_sorsmenedek():
    got = {}

    async def check(b):
        await open_concord(b)
        await b.js(SETUP)
        await b.until("!!document.getElementById('cc-discovery')")
        await b.js("document.getElementById('cc-discovery').click()")
        await wait_for(b, "[...document.querySelectorAll('.cc-public-room')].some(r=>r.innerText.includes('Rin anime'))", 30)
        await b.js("[...document.querySelectorAll('.cc-public-room')].find(r=>r.innerText.includes('Rin anime')).click()")
        await wait_for(b, ROOMS + ".some(r=>r.communityId===__ids.rin && r.cord && r.cord.hydrated)", 45)
        await asyncio.sleep(2)
        got['rooms'] = await b.js(ROOMS + ".map(r=>({name:r.name,id:r.communityId,aliases:r.inviteAliases||[]}))")
        got['active'] = await b.js(ROOMS + "[+localStorage.getItem('pc.concord.active.v1.'+__PC.me().pubkey)]?.communityId")
        got['ids'] = await b.js('__ids')
        # A reload lists them apart: the list's own de-duplication must not merge them back.
        got['after_unique'] = await b.js("PCConcord.uniqueRooms(" + ROOMS + ").map(r=>r.communityId)")

    asyncio.run(desktop.with_browser('online', '', check, T.tunnel(__dirRelayUp=True, __upRelays=['wss://ext-a.invalid', *CORD_RELAYS])))
    ids, rooms = got['ids'], got['rooms']
    by = {r['id']: r for r in rooms}
    assert ids['rin'] in by and ids['sors'] in by, ('Rin and Sorsmenedek are not two rooms', rooms)
    assert by[ids['sors']]['name'] == 'Sorsmenedek' and by[ids['rin']]['name'] == 'Rin anime', rooms
    assert ids['rinUrl'] not in by[ids['sors']]['aliases'], ('Sorsmenedek still carries Rin\'s invite', rooms)
    assert got['active'] == ids['rin'], ('Join opened another community', got['active'], rooms)
    assert sorted(got['after_unique']) == sorted([ids['rin'], ids['sors']]), ('the list merges them again on load', got)
