"""CONCORD READS AND WRITES THIS NODE'S RELAY — the client half, in the real bundled app.

"for telegram, we store everything on the relay and it works great. any way to make our concord
implementation do that also so each client don't have to communicate with other concord relays?"

The relay network is URL-aware (tests/client/concord_mirror_fixture.py): this node's relay holds what the
server's mirror would hold, and EVERY other relay is down. Everything else is the shipped client — its
relay.js, its signer, its reader, its DOM. Before this change each test failed: the room read only its
own (dead) relays and showed nothing; a new community listed the four public CORD relays; and the
directory said "No public communities found" whenever its relay had been skipped.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client.concord_mirror_fixture import INSTALL_NETWORK

HOME = 'wss://fixture.invalid'
CORD_RELAYS = ['wss://jskitty.com/nostr', 'wss://asia.vectorapp.io/nostr', 'wss://nostr.computingcache.com']
CHROME = Path('/opt/google/chrome/chrome').exists()


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


async def wait_for(b, expression, seconds=30):
    for _ in range(int(seconds * 10)):
        if await b.js(expression):
            return
        await asyncio.sleep(.1)
    raise AssertionError({'waiting': expression, 'body': await b.js('document.body.innerText.slice(-1500)')})


def init(**flags):
    pre = ''.join(f'window.{k}={json.dumps(v)};' for k, v in flags.items())
    return ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),osMode:false}));"
            + pre + INSTALL_NETWORK)


# A community that lives ELSEWHERE: minted with two external relays (both down), its genesis and one
# message already held by this node's relay — as the server's mirror holds them.
MINT_EXTERNAL = r'''(async()=>{
  const relays=['wss://ext-a.invalid','wss://ext-b.invalid'];
  const made=await PosterCord.createCommunity({owner:__PC.me().pubkey,name:'Elsewhere',relays,base:'https://cord.invalid',signEvent:__PC.signTemplate});
  const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot};
  const controls=made.events.filter(e=>e.kind===1059);
  const info=PosterCordReader.inspectControl(bundle,controls);
  const general=info.channels.find(c=>c.name==='general');
  const msg=await PosterCordReader.createChatWrap(bundle,controls,general.id,'Mirrored hello from elsewhere',__PC.me().pubkey,__PC.signTemplate);
  __home.push(...made.events,msg.wrap);
  const room={name:'Elsewhere',communityId:made.communityId,naddr:'cord-elsewhere',url:made.url,channels:info.channels.map(c=>({name:c.name,id:c.id,private:!!c.private})),cord:{bundle}};
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify([room]));
  localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
  window.__bundle=bundle;
  return {relays:bundle.relays,streams:[...info.controlPubkeys,...info.channels.flatMap(c=>c.streamPubkeys)]};
})()'''


async def open_concord(b):
    await desktop.login(b)
    await b.js("__PC.switchMessagesTab('concord')")
    await b.until('!!window.PosterCordReader && !!window.PosterCord && !!window.PCConcord')


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_a_joined_external_room_loads_from_this_nodes_relay_with_every_external_relay_down():
    async def check(b):
        await open_concord(b)
        minted = await b.js(MINT_EXTERNAL)
        assert minted['relays'] and HOME not in minted['relays'], minted
        await b.js('PCConcord.render()')
        await wait_for(b, "document.body.innerText.includes('Mirrored hello from elsewhere')", 45)
        # It asked THIS node to mirror it: the community's relays and its streams, nothing else.
        regs = await b.js('__registrations')
        assert regs, 'no mirror registration reached this node'
        last = regs[-1]
        assert last['op'] == 'mirror' and sorted(last['relays']) == sorted(minted['relays']), last
        assert set(minted['streams']) <= set(last['authors']), last
        assert set(last) <= {'v', 'op', 'group', 'relays', 'authors', 'pubkey'}, last
        assert len(last['group']) == 64 and 'Elsewhere' not in json.dumps(last)
        # A message written here goes to this node's relay, which carries it upstream.
        before = await b.js('__homeWrites.filter(e=>e.kind===1059).length')
        await b.js("(()=>{const el=document.querySelector('#cc-input');el.focus();el.value='Sent through this node';el.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await b.js("document.querySelector('#cc-send').click()")
        await wait_for(b, f'__homeWrites.filter(e=>e.kind===1059).length>{before}', 30)
        streams = json.dumps(minted['streams'])
        assert await b.js(f'__homeWrites.filter(e=>e.kind===1059).every(e=>{streams}.includes(e.pubkey))')
        await wait_for(b, "document.body.innerText.includes('Sent through this node')", 20)
        assert 'Delivery needs attention' not in await b.js('document.body.innerText')
    asyncio.run(desktop.with_browser('online', '', check, init()))


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_when_this_node_cannot_answer_the_room_says_so_instead_of_claiming_it_is_empty():
    """The node refuses (`unavailable:`) and the community's own relays are down: never "the start of this
    encrypted channel" — the read that nobody answered is reported as one."""
    async def check(b):
        await open_concord(b)
        await b.js(MINT_EXTERNAL)
        await b.js("__home.splice(0)")                  # what the node holds does not matter: it refuses
        await b.js('PCConcord.render()')
        await wait_for(b, "document.body.innerText.includes('could not be read')", 40)
        text = await b.js('document.body.innerText')
        assert 'This is not the same as the channel being empty' in text, text[-1200:]
        assert 'This is the start of this encrypted channel' not in text, text[-1200:]
        assert 'Mirrored hello' not in text
        assert await b.js("__dials.some(u=>u.startsWith('wss://ext-'))"), 'the fallback never asked the community relays'
    asyncio.run(desktop.with_browser('online', '', check, init(__homeMirror='unavailable')))


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_a_community_created_on_an_instance_lists_only_this_nodes_relay():
    async def check(b):
        await open_concord(b)
        await b.js('PCConcord.render()')
        await b.until("!!document.querySelector('#cc-create')")
        await b.js("document.querySelector('#cc-create').click()")
        await b.until("!!document.querySelector('#cc-community-name')")
        await b.js("(()=>{const n=document.querySelector('#cc-community-name');n.value='Made here';n.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await b.js("document.querySelector('#cc-create-go').click()")
        await wait_for(b, "(JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]')).some(r=>r.name==='Made here')", 45)
        room = await b.js("JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)).find(r=>r.name==='Made here')")
        assert room['cord']['bundle']['relays'] == [HOME], room['cord']['bundle']['relays']
        # its genesis is on this node's relay; nothing of it was handed to another relay
        wraps = await b.js('__homeWrites.filter(e=>e.kind===1059).length')
        assert wraps >= 3, wraps
        # Not MIRRORED (its home is here) -- REPLICATED: the node is asked to copy it to its backup relays
        # (concord_mirror `op: replicate`, 2026-10-21 "make sure posterchan room federates"), naming stream keys only.
        await wait_for(b, "__registrations.some(r=>r.op==='replicate')", 45)
        regs = await b.js('__registrations')
        assert not [r for r in regs if r.get('op') == 'mirror'], ('a community made here was registered for mirroring', regs)
        rep = next(r for r in regs if r.get('op') == 'replicate')
        assert rep.get('authors') and all(len(a) == 64 for a in rep['authors']) and 'relays' not in rep, rep
        # The public CORD relays were UP (they still carry the account's membership vault, the invite
        # registry and the directory listing) -- and not one of the community's own wraps reached them.
        elsewhere = await b.js('[...__upWrites.map(x=>x.ev),...__dirWrites].filter(e=>e.kind===1059).length')
        assert elsewhere == 0, elsewhere
    asyncio.run(desktop.with_browser('online', '', check, init(__dirRelayUp=True, __upRelays=CORD_RELAYS)))


# --- the community directory ---------------------------------------------------------------------------

INVITES = r'''(async()=>{const sk=new Uint8Array(32).fill(7),owner=NostrTools.getPublicKey(sk),sign=async t=>NostrTools.finalizeEvent(t,sk);
  const out=[];
  for(const i of [0,1,2]){
    const made=await PosterCord.createCommunity({owner,name:'Directory room '+i,relays:['wss://ext-a.invalid'],base:'https://armada.buzz',signEvent:sign});
    out.push(await sign({kind:1,created_at:Math.floor(Date.now()/1000)-i*60,content:'Directory room '+i+'\n\n'+made.url,tags:[['t','concord']]}));
  }
  window.__directory=out;window.__dirHold=false;return out.length;
})()'''


def directory_check(expect):
    async def check(b):
        await open_concord(b)
        await b.js(INVITES)
        await b.js('PCConcord.render()')
        if expect == 'listed':
            await wait_for(b, "document.querySelectorAll('.cc-public-room').length>0", 30)
            text = await b.js('document.body.innerText')
            assert 'No public communities found' not in text
        else:
            await wait_for(b, "document.body.innerText.includes('Couldn’t reach the community directory')", 30)
            text = await b.js('document.body.innerText')
            assert 'No public communities found' not in text
            assert await b.js("!!document.querySelector('#cc-discover-retry')")
            # Retry, with the directory back: the list appears.
            await b.js("window.__dirRelayUp=true")
            await b.js("document.querySelector('#cc-discover-retry').click()")
            await wait_for(b, "document.querySelectorAll('.cc-public-room').length>0", 30)
    return check


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_the_directory_is_read_from_this_nodes_relay():
    asyncio.run(desktop.with_browser('online', '', directory_check('listed'), init(__dirMode='serve', __dirHold=True)))


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_the_directory_relay_holds_invites_and_the_list_shows_them_even_when_it_was_cooling_down():
    """THE REPORTED CASE: this node cannot answer the directory, the directory relay holds the invites — and
    was resting after an earlier failure (or busy with another Concord read). It used to be skipped
    silently and the panel said "No public communities found"."""
    cooled = ("window.addEventListener('DOMContentLoaded',()=>{const t=setInterval(()=>{if(window.Relay&&Relay._queryFromCooldown){"
              "Relay._queryFromCooldown.set('wss://relay.dreamith.to',Date.now()+3600000);Relay._queryFromActive.add('wss://relay.dreamith.to');clearInterval(t);}},5);});")
    asyncio.run(desktop.with_browser('online', '', directory_check('listed'),
                                     init(__dirMode='unavailable', __dirRelayUp=True, __dirHold=True) + cooled))


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_an_unreachable_directory_says_so_and_offers_retry():
    asyncio.run(desktop.with_browser('online', '', directory_check('unreachable'),
                                     init(__dirMode='unavailable', __dirRelayUp=False, __dirHold=True)))


MINT_THREE = r'''(async()=>{
  const rooms=[];
  for(const name of ['First','Second','Third']){
    const relays=['wss://ext-'+name.toLowerCase()+'.invalid'];
    const made=await PosterCord.createCommunity({owner:__PC.me().pubkey,name,relays,base:'https://cord.invalid',signEvent:__PC.signTemplate});
    const bundle={...PosterCord.openInvite(made.url,made.events).bundle,control_root:made.secrets.controlRoot};
    const controls=made.events.filter(e=>e.kind===1059);
    const info=PosterCordReader.inspectControl(bundle,controls);
    __home.push(...made.events);
    // what an earlier visit left behind on this device: the community's control envelopes, cached
    await PCConcordCache.put(JSON.stringify([made.communityId,'control']),controls);
    rooms.push({name,communityId:made.communityId,naddr:'cord-'+name,url:made.url,channels:info.channels.map(c=>({name:c.name,id:c.id,private:!!c.private})),cord:{bundle}});
  }
  localStorage.setItem('pc.concord.rooms.v1.'+__PC.me().pubkey,JSON.stringify(rooms));
  localStorage.setItem('pc.concord.active.v1.'+__PC.me().pubkey,'0');
  return rooms.length;
})()'''


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_every_joined_community_is_handed_to_this_node_not_only_the_one_opened():
    """poster.place's mirror held ONE community (2026-10-13) while a member had joined six: a community was
    registered only when one of its rooms was opened on that device, so every other room was read from its
    own relays by every client. Opening Concord with the first community active must register all three."""
    async def check(b):
        await open_concord(b)
        assert await b.js(MINT_THREE) == 3
        # the next app start, with these communities already joined on this device
        await b.call('Page.reload')
        await b.until('!!window.__PC && !!window.PCConcord')
        await b.until("!!__PC.me()")
        await b.js("__PC.switchMessagesTab('concord')")
        await wait_for(b, 'new Set(__registrations.filter(r=>r.op==="mirror").map(r=>r.group)).size>=3', 45)
        regs = await b.js('__registrations.filter(r=>r.op==="mirror")')
        assert len({r['group'] for r in regs}) == 3, regs
        assert {tuple(r['relays']) for r in regs} >= {('wss://ext-first.invalid',), ('wss://ext-second.invalid',),
                                                      ('wss://ext-third.invalid',)}, regs
    asyncio.run(desktop.with_browser('online', '', check, init()))
