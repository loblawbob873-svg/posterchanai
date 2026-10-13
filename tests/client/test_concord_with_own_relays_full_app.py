"""CONCORD WITH "USE MY OWN RELAYS" ON -- the same checks as test_concord_reads_this_node_full_app.py, with the
account's own relay list switched on (a relay of its own that holds no Concord data).

Asked: "if user chooses to use their own relays, will concord work?" By design Concord reaches this node's
relay by its own address (queryFrom/publishTo open their own sockets), and the pool keeps the instance's relay
beside the user's own -- this proves the result in the shipped app: the joined room, a new community and the
public directory all still work, and none of the room's traffic lands on the user's relay.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client import test_concord_reads_this_node_full_app as base

CHROME = Path('/opt/google/chrome/chrome').exists()
MINE = 'wss://my-own-relay.example'
HOME = base.HOME


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def own_relays(**flags):
    # "use my own relays" ON with one relay of the user's own, which is UP (it accepts and answers EOSE)
    # but holds no Concord data; the device has already answered the one-time relay-switch question.
    pre = ("localStorage.setItem('pc_nostr_settings',JSON.stringify({...JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}'),"
           f"relaysEnabled:true,relays:[{json.dumps(MINE)}],relaySwitchAsked:true}}));")
    ups = list(flags.pop('__upRelays', [])) + [MINE]
    return base.init(__upRelays=ups, **flags).replace(base.INSTALL_NETWORK, pre + base.INSTALL_NETWORK)


async def the_pool_is_mine(b):
    """The own-relays case really is on: the saved switch and the user's relay is a live member of the pool.
    (The pool is the user's relays PLUS the instance's -- app.js connectRelays: a personal list is an addition
    to discovery, not an off switch -- so this node's relay staying in it is the design, not a test leak.)"""
    await base.wait_for(b, f"!!(window.Relay&&Relay._conns&&Relay._conns.has({json.dumps(MINE)}))", 30)
    saved = await b.js("JSON.parse(localStorage.getItem('pc_nostr_settings')||'{}')")
    assert saved.get('relaysEnabled') is True and saved.get('relays') == [MINE], saved


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_a_joined_room_loads_and_sends_through_this_node_with_own_relays_on():
    async def check(b):
        await base.open_concord(b)
        await the_pool_is_mine(b)
        await b.js(base.MINT_EXTERNAL)
        await b.js('PCConcord.render()')
        await base.wait_for(b, "document.body.innerText.includes('Mirrored hello from elsewhere')", 45)
        before = await b.js('__homeWrites.filter(e=>e.kind===1059).length')
        await b.js("(()=>{const el=document.querySelector('#cc-input');el.focus();el.value='Own relays, still here';el.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await b.js("document.querySelector('#cc-send').click()")
        await base.wait_for(b, f'__homeWrites.filter(e=>e.kind===1059).length>{before}', 30)
        await base.wait_for(b, "document.body.innerText.includes('Own relays, still here')", 20)
        assert 'Delivery needs attention' not in await b.js('document.body.innerText')
        # The room's traffic did not go to the user's own relay.
        assert await b.js(f"__upWrites.filter(x=>x.url.replace(/\\/+$/,'')==={json.dumps(MINE)}&&x.ev.kind===1059).length") == 0
    asyncio.run(desktop.with_browser('online', '', check, own_relays()))


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_a_community_created_with_own_relays_on_still_lives_on_this_node():
    async def check(b):
        await base.open_concord(b)
        await the_pool_is_mine(b)
        await b.js('PCConcord.render()')
        await b.until("!!document.querySelector('#cc-create')")
        await b.js("document.querySelector('#cc-create').click()")
        await b.until("!!document.querySelector('#cc-community-name')")
        await b.js("(()=>{const n=document.querySelector('#cc-community-name');n.value='Made here';n.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await b.js("document.querySelector('#cc-create-go').click()")
        await base.wait_for(b, "(JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]')).some(r=>r.name==='Made here')", 45)
        room = await b.js("JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)).find(r=>r.name==='Made here')")
        assert room['cord']['bundle']['relays'] == [HOME], room['cord']['bundle']['relays']
        assert await b.js('__homeWrites.filter(e=>e.kind===1059).length') >= 3
    asyncio.run(desktop.with_browser('online', '', check, own_relays(__dirRelayUp=True, __upRelays=base.CORD_RELAYS)))


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_the_public_directory_lists_with_own_relays_on():
    async def check(b):
        await base.open_concord(b)
        await the_pool_is_mine(b)
        await b.js(base.INVITES)
        await b.js('PCConcord.render()')
        await base.wait_for(b, "document.querySelectorAll('.cc-public-room').length>0", 30)
        assert 'No public communities found' not in await b.js('document.body.innerText')
    asyncio.run(desktop.with_browser('online', '', check, own_relays(__dirMode='serve', __dirHold=True)))
