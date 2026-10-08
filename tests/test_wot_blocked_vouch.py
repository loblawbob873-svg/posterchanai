"""A blocked account vouches for nobody in the web of trust.

2026-10-08: the admin had blocked two botrift.com follow-farm bots, yet a relay prober they followed went on
posting "mc-relay-probe" 1,376 times -- the rebuild kept the blocked hubs OUT of the member set but still
crawled their follow lists, so each probe key counted two vouchers (min_followers = 2) and was admitted. "i
thought i already blocked the narcissist". Runs the shipped WotGate.build with a fake follow graph.
"""
import asyncio
from collections import Counter
from unittest import mock

from app.services.nostr_relay import wot as wot_mod

S1, S2 = "1" * 64, "2" * 64                 # seeds
HUB1, HUB2 = "a" * 64, "b" * 64             # blocked follow-farm bots, followed back by both seeds
R1, R2 = "c" * 64, "d" * 64                 # real people the seeds follow
PROBE, NICE, HALF = "e" * 64, "f" * 64, "9" * 64
DEEP_BAD, DEEP_OK = "7" * 64, "8" * 64

FOLLOWS = {S1: [HUB1, HUB2, R1, R2], S2: [HUB1, HUB2, R1, R2],
           HUB1: [PROBE, HALF, DEEP_BAD], HUB2: [PROBE, DEEP_BAD],      # the farm's only vouchers
           R1: [NICE, HALF], R2: [NICE],
           NICE: [DEEP_OK], PROBE: [DEEP_BAD]}


def _build(blocked, depth=2):
    async def counter(self, upstream, authors, direct, batch, pace):
        c = Counter()
        for a in authors:
            for p in FOLLOWS.get(a, []):
                c[p] += 1
        return c
    gate = wot_mod.WotGate()
    gate.set_blocked(blocked)
    store = mock.Mock()
    store.wot_replace = mock.AsyncMock(return_value=0)
    store.kv_set = mock.AsyncMock()
    with mock.patch.object(wot_mod.WotGate, "_follows_counter", counter):
        asyncio.run(gate.build(store, [], [S1, S2], depth=depth, min_followers=2))
    return gate


def test_what_only_blocked_accounts_vouch_for_is_not_admitted():
    gate = _build([HUB1, HUB2])
    assert not gate.is_member(PROBE), "a blocked hub's follows still admitted the prober"
    assert not gate.is_member(HUB1) and not gate.is_member(HUB2)
    assert gate.is_member(NICE), "two unblocked vouchers must still admit an account"
    assert not gate.is_member(HALF), "one real voucher + one blocked one is one voucher, not two"


def test_unblocked_the_same_graph_admits_as_before():
    gate = _build([])
    assert gate.is_member(PROBE) and gate.is_member(HALF) and gate.is_member(NICE)


def test_the_third_tier_is_not_crawled_from_a_blocked_account():
    gate = _build([HUB1, HUB2, PROBE], depth=3)
    assert not gate.is_member(DEEP_BAD)
