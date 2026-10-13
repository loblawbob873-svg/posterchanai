"""A CONCORD MESSAGE IS DELIVERED WHILE DOZENS OF ROOM SUBSCRIPTIONS HOLD THE TUNNEL.

Reported 2026-10-13: "concord not even sending messages on my android now. broken". Measured on poster.place: one
member's Concord held a live subscription per relay per room, every one of them a tunnel through this node
(`/ws/concord-relay`), the per-member share was 24 (app/services/concord_tunnel.py PER_MEMBER), and every SEND after
it was a socket refused before it opened -- 2,314 refusals in an hour. Fixed by dfb3aa075 (PER_MEMBER=128 + concord.js
`sendOr`, a send the tunnel cannot carry goes direct once).

The test that shipped with the fix (test_concord_send_survives_a_refused_tunnel.py) drives a stub in which the
tunnel is simply "up" or "down"; nothing in the suite had ever opened the REAL number of subscriptions the client
holds. This does: the shipped client, signed in on a phone, with 16 communities spread over 4 relays elsewhere,
every tunnel enforced by the SHIPPED tunnel share (tests/client/realnode_harness.py runs
concord_tunnel.acquire/release in front of a real relay). It waits until the client holds more live tunnels than
the old share allowed, then types a message and presses Send, and asserts what the person sees AND what the room's
relays received.

Fail-first (measured):
  PC_CONCORD_TUNNEL_SRC=<dfb3aa075^ concord_tunnel.py> \
  PC_JOURNEY_OVERRIDE=/static/js/client/concord.js=<dfb3aa075^ concord.js> pytest <this file>
fails: the message never reaches the room's relays.
"""
import asyncio
import json
import time

import pytest

from tests.client import realnode_harness as R

CHROME = pytest.mark.skipif(not R.chrome_ok(), reason='Chrome required')
ROOMS = 16
OLD_SHARE = 24

async def until_tunnels(n, want, seconds=60):
    end = time.time() + seconds
    while time.time() < end:
        if n.home.tunnel_peak >= want or n.home.tunnel_refused:
            return
        await asyncio.sleep(.5)


@CHROME
def test_a_message_reaches_its_room_while_dozens_of_rooms_hold_tunnels():
    async def main():
        with R.Node(far=4) as n:
            async with R.Device(n, 1, phone=True) as d:
                rooms = await R.seed_communities(n, d, ROOMS)
                room0 = rooms[0]['cord']['bundle']['relays']
                await d.b.js("__PC.switchMessagesTab('concord')")
                await until_tunnels(n, OLD_SHARE + 1)
                held = n.home.tunnel_peak
                # The realistic condition, not assumed: the client really did hold more than the old share.
                assert held > OLD_SHARE or n.home.tunnel_refused, ('the fixture never reached the old cap', held)
                before = {f.url: len(f.writes) for f in n.far}
                text = 'hello from a busy phone %d' % int(time.time())
                await R.open_room_channel(d)
                await R.concord_send(d, text)
                end = time.time() + 45
                landed = []
                while time.time() < end and not landed:
                    landed = [f.url for f in n.far if f.url in room0 and
                              any(e.get('kind') == 1059 for e in f.writes[before[f.url]:])]
                    await asyncio.sleep(.5)
                said = await d.toasts()
                body = await d.text()
                assert landed, {'step': 'the message reached the room relays', 'tunnels_peak': held,
                                'refused': n.home.tunnel_refused, 'toasts': said, 'screen': body[-600:]}
                await R.wait(d.b, "document.body.innerText.includes(%s)" % json.dumps(text), 30,
                             'the sent message on screen')
                assert 'Delivery needs attention' not in await d.text(), said
    asyncio.run(main())
