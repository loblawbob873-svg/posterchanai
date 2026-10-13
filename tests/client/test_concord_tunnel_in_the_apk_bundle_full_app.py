"""CONCORD'S TUNNEL FROM THE APK'S BUNDLE: the ticket and the dial must reach THE INSTANCE.

In the APK the client is a bundle served from its own origin; `window.__PC_API_BASE__` names the instance and the
shim in mobile/build-www.sh rewrites every root-relative fetch/WebSocket to it. concord.js fetches the ticket as
`fetch('/client/concord/tunnel-ticket')` and builds the tunnel URL from the HOME RELAY's address
(`tunnelBase`: homeRelayUrl host + `/ws/concord-relay`). The existing tunnel browser test stubs the ticket fetch by
substring match, so it could never tell whether the request left the bundle for the right host.

Driven here through the SHIPPED mobile/build-www.sh bundle on its own origin, pointed at a real local instance
(tests/client/realnode_harness.py), signed in with an nsec on a 390px phone:

  * the ticket request reaches the instance (the shim rewrote it) and the ticket is held;
  * the tunnel URL is on THE INSTANCE: `/ws/concord-relay` is an app route (app/main.py mounts
    app/routers/concord_tunnel.ws_router), not a relay route. On poster.place the relay is wss://poster.place/relay,
    so host == instance and it works. But `client.py:_relay_url` gives a node reached DIRECTLY (a turnkey
    http://host:3051) or over its .onion `ws://<host>:3052/relay` -- the RELAY's own port -- and the relay process
    accepts a WebSocket upgrade on ANY path (nostr_relay/server.py process_request), so the "tunnel" is silently a
    plain connection to this node's own relay: sends meant for a community elsewhere land on (or are refused by)
    the home relay, and reads come back from it. XFAIL until tunnelBase derives from the instance (API base /
    page origin), not from the relay URL.
"""
import asyncio

import pytest

from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')


async def concord_in_the_bundle(n, bu):
    d = R.Device(n, 1, phone=True, url=bu.origin + '/index.html', pre_init=R.instance_is(n.origin))
    await d.__aenter__()
    try:
        b = d.b
        assert await b.js('window.__PC_API_BASE__') == n.origin, 'the bundle is not pointed at the instance'
        await b.js("__PC.switchMessagesTab('concord')")
        await R.wait(b, '!!window.PCConcord && !!window.PCConcord.cordVia', 40, 'Concord loaded in the bundle')
        await R.wait(b, "!!PCConcord.cordVia(window.__PC)", 20, 'a tunnel ticket held')
        via = await b.js("PCConcord.cordVia(window.__PC)('wss://far.example/')")
        return d, via
    except BaseException:
        await d.__aexit__()
        raise


@CHROME
def test_the_ticket_request_leaves_the_bundle_for_the_instance():
    async def main():
        with R.Node() as n, R.Bundle() as bu:
            d, via = await concord_in_the_bundle(n, bu)
            try:
                assert '/client/concord/tunnel-ticket' in R.NODE['seen'], R.NODE['seen'][-20:]
                assert via and R.TICKET in via and '/ws/concord-relay?u=' in via, via
            finally:
                await d.__aexit__()
    asyncio.run(main())


@CHROME
@pytest.mark.xfail(strict=True, reason="REPORT 6 FINDING (open): concord.js tunnelBase builds the tunnel URL from the "
                                       "HOME RELAY's host:port, but /ws/concord-relay is an APP route. On a node whose "
                                       "relay is on its own port (direct access http://host:3051 -> ws://host:3052/relay, "
                                       "or the .onion) the dial hits the relay process, which accepts any path as a "
                                       "plain relay connection.")
def test_the_tunnel_is_dialled_on_the_instance_not_the_relay_port():
    async def main():
        with R.Node() as n, R.Bundle() as bu:
            d, via = await concord_in_the_bundle(n, bu)
            try:
                inst = n.origin.replace('http://', 'ws://')
                assert via.startswith(inst + '/ws/concord-relay?'), {'dialled': via.split('?')[0],
                                                                       'instance': inst, 'relay': n.home.url}
            finally:
                await d.__aexit__()
    asyncio.run(main())
