"""Upstream query answers are tallied per CALLER, so the relay's log says which feature pays for them.

py-spy on server1 (2026-10-18): ~18% of the relay process json-decoded upstream query answers and nothing could say
who asked. Driven against a real (local) relay serving real events.
"""
import asyncio
import tempfile

from tests.os_relay_harness import Instance, OWNER_SK, ev
from app.services.nostr import relay as R


async def some_feature(url):
    return await R.query([url], [{"kinds": [1], "limit": 50}], timeout=5)


def test_frames_and_bytes_are_charged_to_the_feature_that_asked():
    with tempfile.TemporaryDirectory() as tmp:
        inst = Instance(tmp).start()
        try:
            inst.add([ev(OWNER_SK, content="post %d" % i) for i in range(20)])
            R._UP_COST.clear()
            got = asyncio.run(some_feature(inst.relay_url))
            assert len(got) == 20
            key = [k for k in R._UP_COST if k.endswith("some_feature")]
            assert key, R._UP_COST
            q, frames, nbytes = R._UP_COST[key[0]]
            assert q == 1 and frames >= 21 and nbytes > 20 * 100, R._UP_COST
        finally:
            inst.close()
