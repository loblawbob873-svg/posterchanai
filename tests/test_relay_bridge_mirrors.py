"""Fediverse/Bluesky MIRROR accounts are kept off this relay — at every door, and retroactively.

The node's rule is "we do not see bridge users": mostr.pub, momostr.pink, ditto.pub and brid.gy are on
the blocklist and "block bridged posts" is on. Measured 2026-10-06, the relay held 53,212 events from
4,468 bridged accounts anyway — 46,262 of them kind-1111 comments — and a user found it by opening one
(graf@poa.st, mirrored by Mostr) and seeing its relay list and comments with no profile.

Three holes, each pinned below:
  * the bridge is invisible in the domain blocklist's terms. An account was classified by its kind-0
    nip05 (`graf@poa-st.mostr.pub`), and 18 of those 4,468 profiles ever arrived; the `proxy` tag the
    bridge puts on EVERY event points at the ORIGIN server (poa.st), never at mostr.pub;
  * "block bridged posts" only ever covered kinds 1 and 6, so comments, reactions, videos and relay
    lists sailed through;
  * six code paths store events and each carried its own copy of the rules — the thread-ancestor
    fetch and two syncs never checked bridges at all. The rule now sits in `store.add_event`, the one
    door they all use.

What must NOT be caught, also pinned: our own fediverse puppets (same `proxy` tag), operators and
registered users, and the non-social `proxy` users on this relay — torrent indexers (16k events, the
Torrents view), rss / web / x.com / github mirrors.
"""
import asyncio
import json
import os
import uuid

import pytest

from app.services.nostr_relay import bridges
from app.services.nostr_relay.wot import WotGate
from app.services.nostr_relay import thread as relay_thread

psycopg2 = pytest.importorskip("psycopg2")
from tests.test_relay_prune import store_factory, _run  # noqa: E402,F401  (the scratch-schema store)


def _ev(pubkey, kind=1, tags=None, content="x"):
    return {"id": uuid.uuid4().hex + uuid.uuid4().hex, "pubkey": pubkey, "created_at": 1790000000,
            "kind": kind, "tags": tags or [], "content": content, "sig": "0" * 128}


MOSTR = [["proxy", "https://poa.st/objects/1", "activitypub"],
         ["client", "Mostr", "31990:6be3:mostr", "wss://relay.ditto.pub"]]
BSKY = [["proxy", "at://did:plc:abc/app.bsky.feed.post/1", "atproto"]]
TORRENT = [["proxy", "https://nyaa.si/view/2126536", "https://nyaa.si/view/2126536"]]
RSS = [["proxy", "https://example.com/feed.xml", "rss"]]
OURS = [["proxy", "https://mastodon.social/@a/1", "activitypub"], ["fedibridge", "https://mastodon.social/users/a"]]

A, B, C, D, E, OP = ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64, "f" * 64)


# ------------------------------------------------------------------------------------- the signal

def test_a_social_bridge_is_recognised_by_its_own_tag():
    assert bridges.is_social_mirror(_ev(A, 1111, MOSTR))
    assert bridges.is_social_mirror(_ev(A, 10002, BSKY))
    assert bridges.is_social_mirror({"tags": [["proxy", "u", "ActivityPub "]]}), "protocol is case/space tolerant"


def test_the_other_proxy_users_are_not_bridge_users():
    """Torrent indexers put the source URL where the protocol goes; rss/web/x.com/github are feed
    mirrors. None of them is a fediverse or Bluesky person, and catching them would empty Torrents."""
    for tags in (TORRENT, RSS, [["proxy", "u", "x.com"]], [["proxy", "u", "github"]], [["proxy", "u"]], []):
        assert not bridges.is_social_mirror({"tags": tags}), tags
    assert not bridges.is_social_mirror({"tags": [None, "junk", ["proxy"], ["proxy", "", "activitypub"]]})


# -------------------------------------------------------------------------------- the admission rule

class Gate(WotGate):
    """The real gate, with the puppet check made answerable without the bridge secret."""
    def __init__(self, puppets=()):
        super().__init__()
        self._puppets = set(puppets)

    def is_puppet_event(self, ev):
        return ev.get("pubkey") in self._puppets


def test_a_mirror_is_refused_and_its_account_is_marked():
    g = Gate()
    assert relay_thread._admit(_ev(A, 1111, MOSTR), g, {"block_bridged": True}) is False
    assert g.is_bridged(A)
    # …so nothing else it signs gets in either, whichever path carries it and whatever its tags
    assert relay_thread._admit(_ev(A, 1, []), g, {"block_bridged": True}) is False
    assert relay_thread._admit(_ev(A, 0, []), g, {"block_bridged": False}) is False, (
        "an account already known to be a mirror stays out even if the switch is later turned off "
        "mid-session — the gate is what the other rules read, and it was already saying so")


def test_a_followed_mirror_is_still_a_mirror():
    """Member-exemption is what made the old hint rule a no-op: every mirror is followed by somebody."""
    g = Gate()
    g.add_members({A})
    assert relay_thread._admit(_ev(A, 1111, MOSTR), g, {"block_bridged": True}) is False


def test_our_puppets_operators_and_the_switch():
    g = Gate(puppets={B})
    g.set_operator({OP})
    cfg = {"block_bridged": True}
    assert relay_thread._admit(_ev(B, 1, OURS), g, cfg) is True and not g.is_bridged(B), "our own puppet"
    assert relay_thread._admit(_ev(OP, 1, MOSTR), g, cfg) is True and not g.is_bridged(OP), "an operator"
    assert relay_thread._admit(_ev(C, 2003, TORRENT), g, cfg) is True, "a torrent indexer"
    assert relay_thread._admit(_ev(E, 1111, MOSTR), g, {"block_bridged": False}) is True, (
        "the switch is OFF: nothing is refused or marked on this signal")
    assert not g.is_bridged(E)


# ------------------------------------------------------------------------- the one door, for real

def test_every_path_is_refused_at_the_store(store_factory):
    async def body(loop):
        st = store_factory(loop)
        g = Gate()
        st.admit = lambda ev: relay_thread._admit(ev, g, {"block_bridged": True})
        assert await st.add_event(_ev(A, 1111, MOSTR), origin="ancestor") is False
        n = await st.add_events_bulk([_ev(A, 7, MOSTR), _ev(E, 1, []), _ev(C, 2003, TORRENT)], origin="wot")
        assert n == 2, "the bulk path (the backfill) let a mirror through"
        assert await st.add_event(_ev(A, 1, []), origin="wot") is False, "a marked mirror's untagged event"
        got = await st.query([{"authors": [A]}])
        assert got == []
        assert len(await st.query([{"authors": [E, C]}])) == 2
    _run(body)


def test_a_hook_that_breaks_admits(store_factory):
    """It is a filter. A bug in it must not stop the relay storing anything."""
    async def body(loop):
        st = store_factory(loop)
        def boom(ev):
            raise RuntimeError("bug")
        st.admit = boom
        assert await st.add_event(_ev(E, 1, []), origin="wot") is True
    _run(body)


# ------------------------------------------------------------------------- what is already stored

def test_the_purge_removes_mirror_accounts_whole_and_nothing_else(store_factory):
    async def body(loop):
        st = store_factory(loop)
        evs = [
            _ev(A, 1111, MOSTR), _ev(A, 10002, MOSTR), _ev(A, 1, []),       # a mirror: all three go
            _ev(B, 1, OURS),                                                 # our puppet: stays
            _ev(C, 2003, TORRENT),                                           # torrent indexer: stays
            _ev(D, 1111, MOSTR),                                             # mirror, but preserved: stays
            _ev(E, 1, []),                                                   # an ordinary author: stays
        ]
        for ev in evs:
            assert await st.add_event(ev, origin="wot")
        st.set_preserve_pubkeys({D})
        g = Gate()
        removed = await relay_thread._apply_social_mirrors(st, g)
        assert removed == 3
        left = {e["pubkey"] for e in await st.query([{"authors": [A, B, C, D, E]}])}
        assert left == {B, C, D, E}
        assert g.is_bridged(A) and not g.is_bridged(D) and not g.is_bridged(B)
    _run(body)


def test_the_purge_rides_the_existing_block_purge_and_its_switch():
    src = open(relay_thread.__file__, encoding="utf-8").read()
    i = src.index("by_proxy = (await store.delete_by_proxy() or 0) if fresh.get(\"block_bridged\") else 0")
    nxt = src[i:i + 600]
    assert "if fresh.get(\"block_bridged\"):" in nxt and "_apply_social_mirrors(store, gate)" in nxt
    assert "store.admit = lambda ev: _admit(ev, gate, cfg)" in src, "the policy is never installed"
    assert "_mark_social_mirrors(store, gate)" in src[src.index("reload-blocks"):] if "reload-blocks" in src else True
