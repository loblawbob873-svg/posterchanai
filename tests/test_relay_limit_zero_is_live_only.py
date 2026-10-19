"""`limit: 0` returns no stored events -- it is how a client opens a live-only subscription (NIP-01).

2026-10-18: PosterChanDB read the limit as `limit or 500`, so a `{"limit": 0}` REQ was answered with the newest 500
stored events: every live-only subscription from another client (and this node's own push service, which subscribes
to DMs with limit 0) paid for and received 500 old events. Live delivery after EOSE must still work.
"""
import asyncio
import json
import tempfile

from tests.os_relay_harness import Instance, OWNER_SK, FRIEND_SK, ev


def test_limit_zero_returns_nothing_stored_and_still_delivers_live():
    with tempfile.TemporaryDirectory() as tmp:
        inst = Instance(tmp).start()
        try:
            inst.add([ev(OWNER_SK, content="old %d" % i, created_at=1_700_000_000 + i) for i in range(30)])
            from websockets.asyncio.client import connect
            fresh = ev(FRIEND_SK, content="live one")

            async def go():
                async with connect(inst.relay_url) as sub, connect(inst.relay_url) as pub:
                    await sub.recv(); await pub.recv()
                    await sub.send(json.dumps(["REQ", "z", {"kinds": [1], "limit": 0}]))
                    stored = []
                    while True:
                        m = json.loads(await asyncio.wait_for(sub.recv(), 5))
                        if m[0] == "EOSE":
                            break
                        if m[0] == "EVENT":
                            stored.append(m[2]["id"])
                    await pub.send(json.dumps(["EVENT", fresh]))
                    live = None
                    while live is None:
                        m = json.loads(await asyncio.wait_for(sub.recv(), 5))
                        if m[0] == "EVENT":
                            live = m[2]["id"]
                    await sub.send(json.dumps(["REQ", "d", {"kinds": [1]}]))      # no limit: the default still applies
                    n = 0
                    while True:
                        m = json.loads(await asyncio.wait_for(sub.recv(), 5))
                        if m[0] == "EOSE" and m[1] == "d":
                            break
                        if m[0] == "EVENT" and m[1] == "d":
                            n += 1
                    return stored, live, n
            stored, live, n = asyncio.run(go())
            assert stored == [], "limit:0 was answered with %d stored events" % len(stored)
            assert live == fresh["id"], "a live-only subscription must still receive new events"
            assert n == 31, "a filter without a limit must still get the stored events (%d)" % n
        finally:
            inst.close()
