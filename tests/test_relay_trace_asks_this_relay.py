"""Trace asks THIS relay who follows an account (2026-10-22: "Trace is not working ... the public relays did not
answer"; "can't we just use our local relay?").

Measured on server1: every Trace logged "follower lookup failed:" (asyncio's timeout). Eight public relays took 12-24 s
to stream 500 full contact lists each, and gather() gave up on all of them at 20 s. The question Trace answers is why
an account is HERE, and that is our web of trust, whose members' contact lists this relay stores: it answered the same
query for jack and fiatjaf (500 lists each) in 0.23 s and 0.4 s.
"""
import asyncio

import pytest

from app.services import relay_reader, relay_trace
from app.services.nostr import relay as upstream

TARGET = "a" * 64


def _cl(author, at, follows=True):
    return {"id": author[:60] + "%04d" % at, "pubkey": author, "kind": 3, "created_at": at,
            "tags": [["p", TARGET]] if follows else [["p", "c" * 64]], "content": "", "sig": "0" * 128}


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(relay_trace, "_relay_rows", lambda pk: {"groups": [], "recent": []})
    monkeypatch.setattr(relay_trace, "_tiers_for", lambda pks: {})

    async def no_public(*a, **k):
        raise AssertionError("Trace asked the public relays")
    monkeypatch.setattr(upstream, "query", no_public)
    asked = []

    def query(filters, *a, **k):
        asked.append(filters)
        return local.answer() if callable(local.answer) else local.answer
    local.answer = []
    monkeypatch.setattr(relay_reader, "query", query)
    local.asked = asked
    return local


def test_followers_come_from_this_relay_and_no_public_relay_is_asked(local):
    local.answer = [_cl("1" * 64, 10), _cl("2" * 64, 20)]
    facts = asyncio.run(relay_trace.gather(TARGET, hops=0))
    assert sorted(facts["followers"]) == ["1" * 64, "2" * 64]
    assert local.asked and local.asked[0][0]["kinds"] == [3] and local.asked[0][0]["#p"] == [TARGET]


def test_this_relay_unreachable_is_could_not_ask_never_no_followers(local):
    def down():
        raise relay_reader.Unavailable("relay down")
    local.answer = down
    facts = asyncio.run(relay_trace.gather(TARGET, hops=0))
    assert facts["followers"] is None
    signals = " ".join(s["text"] for s in relay_trace.explain(facts)["signals"])
    assert "Could not ask this relay who follows it" in signals


def test_only_the_latest_contact_list_counts(local):
    local.answer = [_cl("1" * 64, 10, follows=True), _cl("1" * 64, 20, follows=False), _cl("2" * 64, 5)]
    facts = asyncio.run(relay_trace.gather(TARGET, hops=0))
    assert facts["followers"] == ["2" * 64], "an unfollow in a newer list must win"
