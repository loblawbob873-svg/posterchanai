"""A RELAY THIS NODE'S MIRROR READS IS NEVER DIALLED BY THE CLIENT FOR READS.

Measured on poster.place 2026-10-29 (`[concord-tunnel] dialled in the last 11 min`): jskitty.com 34, relay.ditto.pub
34, relay.dreamith.to 33, relay.cyberguy.fyi 30 -- every one a short tunnel whose first frame was
`REQ 1059 a1 since limit` (one stream, a catch-up read) -- while data/nostr_relay.status.json showed the node's
mirror connected and synced to all four. The client asked this node only for streams in ITS OWN registration
record (homeServes); a record that is missing, stale or refused -- here: the registration refused -- sent every
catch-up read and every live subscription of the room to each of the community's relays, per stream, every minute.

Now a node that says `strict` in /client/concord/mirror-state is ASKED (`pcms…`) and serves any stream it mirrors.
The real bundled client, the shipped relay.js/concord.js, the tunnel fixture. Fails on the code before it: tunnels
to every community relay in the steady-state window.
"""
import asyncio
import json
from collections import Counter

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client import test_concord_reads_this_node_full_app as base
from tests.client import test_concord_talks_only_to_this_node_full_app as T

RELAYS = ['wss://relay.ditto.pub', 'wss://relay.cyberguy.fyi', 'wss://jskitty.com/nostr', 'wss://asia.vectorapp.io/nostr']

MINT = r'''(async(n)=>{
  const relays=%s,rooms=[];window.__made=[];
  for(let i=0;i<n;i++){
    const made=await PosterCord.createCommunity({owner:__PC.me().pubkey,name:'Mirrored '+i,relays,base:'https://cord.invalid',signEvent:__PC.signTemplate});
    const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot};
    const controls=made.events.filter(e=>e.kind===1059);
    const info=PosterCordReader.inspectControl(bundle,controls);
    const general=info.channels.find(c=>c.name==='general');
    const msg=await PosterCordReader.createChatWrap(bundle,controls,general.id,'Held by this node '+i,__PC.me().pubkey,__PC.signTemplate);
    /* the community's relays hold it too, as they do */
    (window.__upHeld=window.__upHeld||[]).push(...relays.map(url=>({url,ev:msg.wrap})),...made.events.flatMap(ev=>relays.map(url=>({url,ev}))));
    __home.push(...made.events,msg.wrap);
    await PCConcordCache.put(JSON.stringify([made.communityId,'control']),controls);
    __made.push({bundle,controls,general:general.id});
    rooms.push({name:'Mirrored '+i,communityId:made.communityId,naddr:'cord-m'+i,url:made.url,channels:info.channels.map(c=>({name:c.name,id:c.id,private:!!c.private})),cord:{bundle}});
  }
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify(rooms));
  localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
  return true;
})(2)''' % json.dumps(RELAYS)

# A message the live subscription never carried (pushed into the node's store, nobody notified): only a catch-up
# read finds it. And one PUBLISHED to the node, which its live subscribers receive.
SILENT = r'''(async(i,text)=>{const m=__made[i];const w=await PosterCordReader.createChatWrap(m.bundle,m.controls,m.general,text,__PC.me().pubkey,__PC.signTemplate);__home.push(w.wrap);return true;})'''
LIVE = r'''(async(i,text)=>{const m=__made[i];const w=await PosterCordReader.createChatWrap(m.bundle,m.controls,m.general,text,__PC.me().pubkey,__PC.signTemplate);
  const s=new WebSocket('wss://fixture.invalid');s.onopen=()=>s.send(JSON.stringify(['EVENT',w.wrap]));return true;})'''
HELD = "(t=>Object.keys(localStorage).some(k=>k.startsWith('pc.concord.test.')&&String(localStorage.getItem(k)).includes(t))||document.body.innerText.includes(t))"
SINCE = "(window.__tunneled||[]).slice(window.__mark||0).map(x=>x.u)"


def mirror_state(strict):
    return ("(()=>{const f=window.fetch;window.fetch=function(url,opts){if(String(url).includes('/client/concord/mirror-state'))"
            f"return Promise.resolve(new Response(JSON.stringify({{groups:{{}},strict:{json.dumps(strict)}}}),"
            "{status:200,headers:{'Content-Type':'application/json'}}));return f.apply(this,arguments);};})();")


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def run(home_mirror, minutes=3):
    got = {}

    async def check(b):
        await base.open_concord(b)
        assert await b.js(MINT)
        await b.js('PCConcord.render()')
        await base.wait_for(b, '__registrations.length>0', 45)
        await base.wait_for(b, "document.body.innerText.includes('Held by this node 0')", 45)
        await asyncio.sleep(3)
        got['warm_ms'] = await desktop.advance(b, 330000)      # past mirrorAllJoined's 5-minute re-run
        await b.js("window.__mark=(window.__tunneled||[]).length")
        got['virtual_ms'] = await desktop.advance(b, minutes * 60000)
        got['tunneled'] = await b.js(SINCE)
        got['direct'] = await b.js(T.DIRECT)
        # (The page clock is paused between advances: started here, run by the advance.)
        # Messages still arrive: a live one, and two only a catch-up finds (the open room and the other one).
        await b.js(f"void ({LIVE})(0,'Live while open');1")
        await b.js(f"void ({SILENT})(0,'Missed by the subscription');1")
        await b.js(f"void ({SILENT})(1,'Missed in the other room');1")
        await desktop.advance(b, 150000)
        got['arrived'] = {t: await b.js(f"{HELD}({json.dumps(t)})")
                          for t in ('Live while open', 'Missed by the subscription', 'Missed in the other room')}

    asyncio.run(desktop.with_browser('online', '', check, T.tunnel(
        __upRelays=RELAYS, __homeMirror=home_mirror,
        # This device's registration is REFUSED (another member's keeps the node mirroring the community).
        __regRefuse='blocked: at most 20 mirrored communities per account') + mirror_state(True)))
    got['by_relay'] = Counter(u.rstrip('/') for u in got['tunneled'])
    return got


@pytest.mark.skipif(not T.CHROME, reason='Chrome required')
def test_streams_the_node_mirrors_are_read_from_it_not_from_their_relays():
    got = run('serve')
    print('tunnels per relay in 3 min:', dict(got['by_relay']))
    assert got['virtual_ms'] >= 179999, got['virtual_ms']
    assert got['direct'] == [], ('Concord dialled a relay directly', got['direct'])
    polled = {u: n for u, n in got['by_relay'].items() if u in RELAYS}
    assert not polled, ('relays this node mirrors were read through the tunnel', polled)
    assert all(got['arrived'].values()), ('a message did not arrive', got['arrived'])


@pytest.mark.skipif(not T.CHROME, reason='Chrome required')
def test_a_stream_the_node_does_not_mirror_is_still_read_from_its_relays():
    """The node says no (nobody registered it): the community's own relays, through the tunnel, as before."""
    got = run('unavailable', minutes=2)
    print('tunnels per relay in 2 min:', dict(got['by_relay']))
    assert got['direct'] == [], ('Concord dialled a relay directly', got['direct'])
    assert any(u in RELAYS for u in got['by_relay']), ('the fallback to the community relays is gone', got['by_relay'])
