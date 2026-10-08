"""The relay trace: the web-of-trust rebuild RECORDS why each member is in, and the trace says it in words.

"how did it make it to my relay!" (2026-10-08) could not be answered from the relay: the rebuild kept a bare
set. These pin the three halves — the record (built by the real WotGate.build), the words (relay_trace.explain),
and the admin-only endpoint.
"""
import asyncio
import json
from collections import Counter
from unittest import mock

import pytest

from app.services import relay_trace
from app.services.nostr_relay import wot as wot_mod

ZW = "‌⁠​" * 50
S1, S2 = "1" * 64, "2" * 64          # seeds
F1, F2, F3 = "a" * 64, "b" * 64, "c" * 64
FOF, FOFOF = "d" * 64, "e" * 64


def _facts(**kw):
    rows = {"groups": [(1, "wot", 40, 100, 200)], "wot": (1, 0), "tier_row": None,
            "recent": [("hello there friend number %d" % i, "[]", 200 - i) for i in range(40)],
            "profile": '{"name":"x"}'}
    rows.update(kw.pop("rows", {}))
    return {"pubkey": "f" * 64, "rows": rows, "followers": kw.get("followers", []), "tiers": kw.get("tiers", {}),
            "chain": kw.get("chain", [])}


# ------------------------------------------------------------------------------ the record
def test_the_rebuild_records_each_members_tier_and_vouchers():
    follows = {S1: [F1, F2, F3], S2: [F1, F2],             # F1,F2 followed by both seeds; F3 by one
               F1: [FOF], F2: [FOF], F3: [FOF],             # FOF followed by 3 direct follows
               FOF: [FOFOF], F1 + "x": []}

    async def counter(self, upstream, authors, direct, batch, pace):
        c = Counter()
        for a in authors:
            for p in follows.get(a, []):
                c[p] += 1
        return c

    gate = wot_mod.WotGate()
    store = mock.Mock()
    store.wot_replace = mock.AsyncMock(return_value=0)
    store.kv_set = mock.AsyncMock()
    with mock.patch.object(wot_mod.WotGate, "_follows_counter", counter):
        asyncio.run(gate.build(store, [], [S1, S2], depth=3, min_followers=1))
    key, value = store.kv_set.call_args.args
    assert key == wot_mod.WOT_TIERS_KEY
    rec = json.loads(value)
    t = rec["tiers"]
    assert t[S1] == [0, 0] and t[F1] == [1, 2] and t[F3] == [1, 1]
    assert t[FOF] == [2, 3] and t[FOFOF] == [3, 1]
    assert rec["depth"] == 3 and rec["min_followers"] == 1 and rec["built_at"] > 0


def test_a_partial_crawl_that_keeps_the_cache_does_not_overwrite_the_record():
    async def counter(self, upstream, authors, direct, batch, pace):
        return Counter({F1: 1})
    gate = wot_mod.WotGate()
    gate._members = frozenset("%064x" % i for i in range(100))
    store = mock.Mock(wot_replace=mock.AsyncMock(), kv_set=mock.AsyncMock())
    with mock.patch.object(wot_mod.WotGate, "_follows_counter", counter):
        asyncio.run(gate.build(store, [], [S1], depth=1))
    assert gate.last_build_partial and not store.kv_set.called


# ------------------------------------------------------------------------------ the words
@pytest.mark.parametrize("tier,needle,tone", [
    ([0, 0], "seed accounts", "ok"),
    ([1, 2], "followed directly by 2 of your seed accounts", "info"),
    ([2, 5], "friend of a friend: 5 accounts", "warn"),
    ([3, 6], "three hops out: 6 friends-of-friends", "warn"),
])
def test_the_headline_names_the_rule_that_admitted_it(tier, needle, tone):
    r = relay_trace.explain(_facts(rows={"tier_row": (tier, 1, 3, 3)}))
    assert needle in r["headline"] and r["tone"] == tone, r["headline"]


def test_hidden_payload_notes_make_it_bad_and_say_so():
    r = relay_trace.explain(_facts(rows={"tier_row": ([3, 6], 1, 3, 3),
                                         "recent": [("hi " + ZW, '[["t","webmesh-v1-nodes"]]', 1000 - i)
                                                    for i in range(30)], "profile": None}))
    assert r["tone"] == "bad"
    texts = " ".join(s["text"] for s in r["signals"])
    assert "30 of its last 30 notes are hidden-character payloads" in texts
    assert "No profile" in texts and r["stored"]["top_tags"] == ["webmesh-v1-nodes"]


def test_could_not_ask_is_never_reported_as_no_followers():
    asked = " ".join(s["text"] for s in relay_trace.explain(_facts(followers=[]))["signals"])
    unasked = relay_trace.explain(_facts(followers=None))
    assert "No follower found" in asked
    assert "Could not ask" in " ".join(s["text"] for s in unasked["signals"]) and unasked["followers"]["found"] is None


def test_blocked_and_member_and_absent():
    assert relay_trace.explain(_facts(), blocked=True)["tone"] == "bad"
    m = relay_trace.explain(_facts(), member={"qualified": True, "addresses": ["bob@poster.place"]})
    assert m["tone"] == "ok" and "bob@poster.place" in m["headline"]
    gone = relay_trace.explain(_facts(rows={"groups": [], "wot": None, "recent": []}))
    assert "Not on this relay" in gone["headline"]
    pulled = relay_trace.explain(_facts(rows={"groups": [(1, "ancestor", 3, 1, 2)], "wot": None}))
    assert "parents of replies" in pulled["headline"]


def test_members_with_no_recorded_tier_still_say_who_vouches():
    r = relay_trace.explain(_facts(followers=[F1, F2, F3], tiers={F1: [2, 9], F2: None}))
    assert "2 accounts already in it follow it" in r["headline"]
    shown = r["followers"]["shown"]
    assert [f["pubkey"] for f in shown] == [F1, F2, F3]            # tiered member, member, outsider
    assert shown[2]["in_wot"] is False and r["followers"]["in_wot"] == 2


# ------------------------------------------------------------------------------ the endpoint
def test_the_endpoint_demands_an_admin_proof_bound_to_this_account_and_purpose():
    from app.routers import client
    with mock.patch.object(client, "_verify_admin_auth", return_value=None) as v:
        r = asyncio.run(client.relay_trace(client.RelaySyncReq(target="f" * 64, auth="x"), db=mock.Mock()))
    assert r.status_code == 403
    assert v.call_args.args[2:] == ("f" * 64, "relay-trace")
