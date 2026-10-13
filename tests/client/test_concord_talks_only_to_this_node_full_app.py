"""CONCORD TALKS ONLY TO THIS NODE -- in the real bundled app, with a relay network that tells a direct dial
from one carried by this node's tunnel (`/ws/concord-relay`, app/services/concord_tunnel.py).

The owner asked for it days before it was done: "any way to make our concord implementation do that also so
each client don't have to communicate with other concord relays?" The relay mirror covered reads it held;
the membership vault on the CORD relays, the directory, invites and every room hosted elsewhere were still
dialled directly. Each test here fails on the code before this change: it records EVERY socket the page opens
and allows only this node's relay and the tunnel.
"""
import asyncio
import json
from pathlib import Path

import pytest
from tests.client import test_desktop_offline_full_app as desktop
from tests.client import test_concord_reads_this_node_full_app as base
from tests.client.concord_mirror_fixture import INSTALL_NETWORK

CHROME = Path('/opt/google/chrome/chrome').exists()
HOME = base.HOME
TICKET = 'f' * 64 + '.9999999999.' + 'a' * 40

# The tunnel, as the network sees it: a socket to HOME/ws/concord-relay?u=<relay>&t=<ticket> behaves like
# <relay> (up or down exactly as a direct dial would), is recorded in __tunneled, and is refused for a ticket
# the node did not mint.
TUNNEL_NETWORK = INSTALL_NETWORK.replace(
    "constructor(url){super();this.url=String(url);this.readyState=0;const u=norm(url);__dials.push(u);",
    "constructor(url){super();this.url=String(url);this.readyState=0;let real=String(url);__dials.push(norm(url));(window.__dialFrom=window.__dialFrom||[]).push({u:norm(url),s:String(new Error().stack||'')});"
    "if(real.startsWith(HOME+'/ws/concord-relay?')){const q=new URL(real.replace(/^ws/,'http')).searchParams;"
    "(window.__tunneled=window.__tunneled||[]).push({u:q.get('u'),t:q.get('t')});"
    "if(q.get('t')!==window.__tunnelTicket){setTimeout(()=>{this.readyState=3;this.fire('error',{});this.fire('close',{});},10);this.kind='refused';return;}"
    "real=q.get('u');}const u=norm(real);")
assert TUNNEL_NETWORK != INSTALL_NETWORK, 'the network fixture changed shape -- update the tunnel hook'
# A refused socket must not run the constructor's own open timer, which would mark it open again.
TUNNEL_NETWORK = TUNNEL_NETWORK.replace(
    "setTimeout(()=>{\n      if(this.kind==='ext'",
    "if(this.kind==='refused')return;setTimeout(()=>{\n      if(this.kind==='ext'")


@pytest.fixture(scope='module', autouse=True)
def bundled_assets():
    yield from desktop.bundle.__wrapped__()


def tunnel(ticket=TICKET, **flags):
    serve = ("(()=>{const f=window.fetch;window.fetch=function(url,opts){if(String(url).includes('/client/concord/tunnel-ticket'))"
             f"return Promise.resolve(new Response(JSON.stringify({{ticket:{json.dumps(ticket)},ttl:3600}}),"
             "{status:200,headers:{'Content-Type':'application/json'}}));return f.apply(this,arguments);};})();")
    pre = ''.join(f'window.{k}={json.dumps(v)};' for k, v in flags.items()) + f'window.__tunnelTicket={json.dumps(ticket)};'
    return (base.init().replace(INSTALL_NETWORK, '') + pre + TUNNEL_NETWORK + serve)


# Only CONCORD's dials: the rest of the app (profile and relay-list lookups) is not what this asks about. A dial
# is Concord's when concord.js is on the stack that opened it.
DIRECT = ("(window.__dialFrom||[]).filter(d=>d.u!==%s&&!d.u.startsWith(%s+'/ws/concord-relay')&&/concord\\.js/.test(d.s)).map(d=>d.u+' <- '+(d.s.split('\\n').find(l=>/concord\\.js/.test(l))||'').trim())"
          % (json.dumps(HOME), json.dumps(HOME)))
ALL_DIRECT = ("(window.__dialFrom||[]).filter(d=>d.u!==%s&&!d.u.startsWith(%s+'/ws/concord-relay')).map(d=>d.u+' <- '+d.s.split('\\n').slice(1,6).join(' | '))"
              % (json.dumps(HOME), json.dumps(HOME)))


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_creating_a_community_reaches_the_cord_relays_only_through_this_node():
    """Creating a community writes the account's membership to the public CORD relays -- through the tunnel."""
    async def check(b):
        await base.open_concord(b)
        await b.js('PCConcord.render()')
        await b.until("!!document.querySelector('#cc-create')")
        await b.js("document.querySelector('#cc-create').click()")
        await b.until("!!document.querySelector('#cc-community-name')")
        await b.js("(()=>{const n=document.querySelector('#cc-community-name');n.value='Made here';n.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await b.js("document.querySelector('#cc-create-go').click()")
        await base.wait_for(b, "(JSON.parse(localStorage.getItem('pc.concord.rooms.v1.'+__PC.me().pubkey)||'[]')).some(r=>r.name==='Made here')", 45)
        await base.wait_for(b, "(window.__tunneled||[]).some(x=>%s.includes(x.u))" % json.dumps(base.CORD_RELAYS), 30)
        print('ALL DIRECT', json.dumps(await b.js(ALL_DIRECT), indent=0)[:6000])
        assert await b.js(DIRECT) == [], 'Concord dialled a relay directly'
        assert await b.js("__upWrites.length") > 0, 'nothing reached the CORD relays at all'
    asyncio.run(desktop.with_browser('online', '', check, tunnel(__dirRelayUp=True, __upRelays=base.CORD_RELAYS)))


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_the_directory_fallback_goes_through_this_node():
    """This node cannot answer the directory; the directory relay can -- and is asked through the tunnel."""
    async def check(b):
        await base.open_concord(b)
        await b.js(base.INVITES)
        await b.js('PCConcord.render()')
        await base.wait_for(b, "document.querySelectorAll('.cc-public-room').length>0", 40)
        assert await b.js("(window.__tunneled||[]).some(x=>x.u.startsWith('wss://relay.dreamith.to'))")
        assert await b.js(DIRECT) == [], 'Concord dialled a relay directly'
    asyncio.run(desktop.with_browser('online', '', check, tunnel(__dirMode='unavailable', __dirRelayUp=True, __dirHold=True)))


@pytest.mark.skipif(not CHROME, reason='Chrome required')
def test_a_node_that_cannot_carry_it_leaves_concord_working_directly():
    """{ticket:''} (no proxy on the node): the old behaviour, direct -- never a Concord that cannot reach anything."""
    async def check(b):
        await base.open_concord(b)
        await b.js(base.INVITES)
        await b.js('PCConcord.render()')
        await base.wait_for(b, "document.querySelectorAll('.cc-public-room').length>0", 40)
        assert not await b.js("(window.__tunneled||[]).length")
    asyncio.run(desktop.with_browser('online', '', check, tunnel(ticket='', __dirMode='unavailable', __dirRelayUp=True, __dirHold=True)))
