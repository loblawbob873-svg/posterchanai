"""A COMMUNITY ONE OF WHOSE RELAYS REFUSES THIS NODE IS READ FROM THIS NODE PLUS THAT RELAY -- NOT FROM ALL OF THEM.

Measured on poster.place 2026-10-14: two of the owner's communities list relay.damus.io, which gates kind-1059
behind an AUTH as the stream key that only members hold, so the mirror can never read it. The node answered every
read of those streams `unavailable:`, and every device then read ALL of the community's relays itself, through the
tunnel, once a minute per stream: ~64 tunnels a minute per device, 767 in ten minutes from one account.

The mirror's registration reply now names the relays it cannot read ("read directly: …"); the client asks the node
for its copy with a `pcmp…` subscription (served despite the refusal) and reads ONLY the named relays itself. The
real bundled client, the shipped relay.js/concord.js, a home relay that serves `pcmp` reads and refuses the rest
(`__homeMirror='partial'`), and a refusing relay that holds a message nobody else has.
"""
import asyncio
import json

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client import test_concord_reads_this_node_full_app as base
from tests.client import test_concord_talks_only_to_this_node_full_app as T

REFUSER = 'wss://refuser.example'
RELAYS = base.CORD_RELAYS + [REFUSER]

MINT = r'''(async()=>{
  const relays=%s;
  const made=await PosterCord.createCommunity({owner:__PC.me().pubkey,name:'Partly here',relays,base:'https://cord.invalid',signEvent:__PC.signTemplate});
  const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot};
  const controls=made.events.filter(e=>e.kind===1059);
  const info=PosterCordReader.inspectControl(bundle,controls);
  const general=info.channels.find(c=>c.name==='general');
  const here=await PosterCordReader.createChatWrap(bundle,controls,general.id,'Held by this node',__PC.me().pubkey,__PC.signTemplate);
  const there=await PosterCordReader.createChatWrap(bundle,controls,general.id,'Only on the refusing relay',__PC.me().pubkey,__PC.signTemplate);
  __home.push(...made.events,here.wrap);
  window.__upHeld=[{url:%s,ev:there.wrap}];
  await PCConcordCache.put(JSON.stringify([made.communityId,'control']),controls);
  const room={name:'Partly here',communityId:made.communityId,naddr:'cord-partly',url:made.url,channels:info.channels.map(c=>({name:c.name,id:c.id,private:!!c.private})),cord:{bundle}};
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));
  localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
  return true;
})()''' % (json.dumps(RELAYS), json.dumps(REFUSER))

TUNNELED_SINCE_MARK = "(window.__tunneled||[]).slice(window.__mark||0).map(x=>x.u)"


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


@pytest.mark.skipif(not T.CHROME, reason='Chrome required')
def test_only_the_relay_this_node_cannot_read_is_read_through_the_tunnel():
    got = {}

    async def check(b):
        await base.open_concord(b)
        assert await b.js(MINT)
        await b.js('PCConcord.render()')
        await base.wait_for(b, '__registrations.some(r=>r.op==="mirror")', 45)
        # Both halves on screen: this node's copy AND the message only the refusing relay holds.
        await base.wait_for(b, "document.body.innerText.includes('Held by this node')", 45)
        await base.wait_for(b, "document.body.innerText.includes('Only on the refusing relay')", 45)
        # Steady state: a minute and a bit of live ticks (each refresh is rate-limited to once a minute).
        await asyncio.sleep(10)
        await b.js("window.__mark=(window.__tunneled||[]).length")
        # 70 seconds of the page's clock without 70 seconds of the gate's: Chrome's virtual time runs the timers
        # (the minute-rate-limited refreshes included) as fast as the page can do the work. Measured below.
        got['virtual_ms'] = await desktop.advance(b, 70000)
        got['tunneled'] = await b.js(TUNNELED_SINCE_MARK)
        got['direct'] = await b.js(T.DIRECT)

    asyncio.run(desktop.with_browser('online', '', check, T.tunnel(
        __upRelays=RELAYS, __homeMirror='partial', __regReply='mirroring: 3 stream(s) from 3 relay(s); read directly: ' + REFUSER)))
    assert got['virtual_ms'] >= 69999, ('the page clock did not run the full window', got['virtual_ms'])
    assert any(u.rstrip('/') == REFUSER for u in got['tunneled']), ('no refresh ran in the window -- the check below would pass vacuously', got)
    assert got['direct'] == [], ('Concord dialled a relay directly', got['direct'])
    others = sorted({u for u in got['tunneled'] if u.rstrip('/') != REFUSER})
    assert not others, ('relays this node reads were read again through the tunnel', others, len(got['tunneled']))
