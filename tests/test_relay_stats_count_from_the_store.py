"""SERVER STATS COUNT FROM THE STORE THE RELAY ACTUALLY RUNS ON.

aggregates.source() picked PosterChanDB only when a MIRROR beside Postgres was serving, and otherwise opened
`store._conn()` -- which, on a relay whose ONLY store is PosterChanDB (both nodes since 2026-10-10), raises
"there is no Postgres". Measured on server1 2026-10-13: `aggregates.ask("server-stats")` answered
Unavailable, so Discover -> Server Stats, the NostrStats bot and the admin's relay trace had nothing to show.
Every existing test handed aggregates a ready-made source and never asked the relay's own store.
"""
import asyncio
import json
import time

from app.services.nostr_relay import aggregates as A
from tests.relay_agg_fixture import ev, pk
from tests.relay_backends import pcdb_factory

NOW = int(time.time())


def test_the_relays_own_store_answers_server_stats_and_the_trace():
    A._CACHE.clear()                  # answers are cached per process; another test's answer is not this store's

    async def go(loop, make):
        st = make(loop)
        evs = [ev("alice", 1, NOW - 60), ev("bob", 1, NOW - 120), ev("bob", 7, NOW - 200),
               ev("alice", 0, NOW - 300, content=json.dumps({"nip05": "alice@x.example"}))]
        for e, origin in evs:
            assert await st.add_event(e, origin=origin)
        stats = A.compute(st, "server-stats", {}, now=NOW)
        assert stats["backend"] == "posterchandb"
        assert stats["windows"]["minute"]["people"] == 2
        activity = A.compute(st, "nip05-activity", {"since": NOW - 86400}, now=NOW)
        assert pk("alice") in activity["nip05"]
        await st.wot_replace([pk("bob")])
        trace = A.compute(st, "trace", {"pubkey": pk("bob")}, now=NOW)
        assert trace["groups"] and trace["wot"] and trace["wot"][0] == 1, trace
        assert A.compute(st, "wot-tiers", {"pubkeys": [pk("bob"), pk("alice")]}, now=NOW) == {pk("bob"): None}
    with pcdb_factory() as make:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(go(loop, make))
        finally:
            loop.close()
