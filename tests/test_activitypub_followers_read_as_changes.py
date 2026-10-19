"""Delivery reads a member's followers as what CHANGED, not the whole list per federated event.

2026-10-18: every federated event re-read the member's entire follower list (~235 documents, each decrypted) -- the
largest of this node's own reads through its relay. A follower is never deleted, only replaced by a tombstone, so the
documents written since the last read are the complete change: follows, refollows and unfollows alike.
"""
import asyncio
import time

import pytest

from app.services import nostr_store
from app.services.activitypub import state


class FakeDocs:
    def __init__(self):
        self.docs = {}                       # d_tag -> (doc, created_at)
        self.reads = []

    async def list_docs(self, port, prefix, *, seckey=None, strict=False, limit=5000, since=None, **kw):
        out = {d: v for d, (v, at) in self.docs.items() if d.startswith(prefix) and (since is None or at >= since)}
        self.reads.append(("delta" if since else "full", len(out)))
        return out

    def put(self, member, actor, doc, at):
        self.docs["%s%s:%s" % (state._FOLLOWER_PREFIX, member, state._h(actor))] = (dict(doc, actor=actor), at)


@pytest.fixture
def fake(monkeypatch):
    f = FakeDocs()
    monkeypatch.setattr(nostr_store, "list_docs", f.list_docs)
    monkeypatch.setattr(state, "_seckey", lambda: b"\1" * 32)
    monkeypatch.setattr(state, "_port", lambda: 1)
    state._FOLLOWERS.clear()
    return f


def _actors(member):
    return sorted(d["actor"] for d in asyncio.run(state.followers(member)))


def test_after_the_first_read_only_changes_are_read_and_unfollows_take_effect(fake, monkeypatch):
    m, now = "a" * 64, int(time.time())
    for i in range(200):
        fake.put(m, "https://s%d.example/u" % i, {"inbox": "https://s%d.example/inbox" % i}, now - 3600)
    assert len(_actors(m)) == 200 and fake.reads == [("full", 200)]
    for _ in range(20):                                       # twenty federated events, nothing changed
        _actors(m)
    assert all(kind == "delta" and n == 0 for kind, n in fake.reads[1:]), fake.reads
    fake.put(m, "https://new.example/u", {"inbox": "https://new.example/inbox"}, int(time.time()))
    fake.put(m, "https://s3.example/u", {"gone": True}, int(time.time()))          # an unfollow: a tombstone
    got = _actors(m)
    assert "https://new.example/u" in got and "https://s3.example/u" not in got and len(got) == 200
    assert fake.reads[-1] == ("delta", 2)


def test_a_full_read_still_happens_hourly_and_a_failed_delta_is_not_nobody(fake, monkeypatch):
    m = "b" * 64
    fake.put(m, "https://x.example/u", {"inbox": "https://x.example/inbox"}, int(time.time()) - 10)
    _actors(m)
    state._FOLLOWERS[m]["full"] -= state._FOLLOWERS_FULL_EVERY + 1
    _actors(m)
    assert fake.reads[-1][0] == "full"

    async def down(*a, **k):
        raise ConnectionError("relay down")
    monkeypatch.setattr(nostr_store, "list_docs", down)
    with pytest.raises(ConnectionError):
        asyncio.run(state.followers(m))                          # strict: "could not ask", never []


def _put(fake, prefix, key, doc, at):
    fake.docs[prefix + key] = (doc, at)


def test_the_cross_member_reads_are_changes_too(fake, monkeypatch):
    """`qstate-` was the relay's largest answer stream: everybody's following and everybody's followers re-read in
    full every minute, by the inbox gate (app) and the everyone pass (worker). After the first read, only changes."""
    now = int(time.time())
    for i in range(150):
        _put(fake, state._FOLLOWING_PREFIX, "%s:%d" % ("c" * 64, i), {"actor": "https://f%d.example/u" % i}, now - 3600)
        _put(fake, state._FOLLOWER_PREFIX, "%s:%d" % ("d" * 64, i), {"actor": "https://g%d.example/u" % i}, now - 3600)
    state.forget_followed_cache()
    state._nonlocal_cache["at"] = 0.0
    assert len(asyncio.run(state.followed_actors(max_age=0))) == 150
    assert asyncio.run(state.nostr_users_with_followers(max_age=0)) == frozenset({"d" * 64})
    fake.reads.clear()
    for _ in range(10):
        asyncio.run(state.followed_actors(max_age=0))
        asyncio.run(state.nostr_users_with_followers(max_age=0))
    assert fake.reads and all(k == "delta" and n == 0 for k, n in fake.reads), fake.reads
    # an unfollow (a tombstone) and a new follow both arrive through the delta
    _put(fake, state._FOLLOWING_PREFIX, "%s:%d" % ("c" * 64, 0), {"actor": "https://f0.example/u", "gone": True}, int(time.time()))
    _put(fake, state._FOLLOWING_PREFIX, "%s:new" % ("e" * 64), {"actor": "https://new.example/u"}, int(time.time()))
    got = asyncio.run(state.followed_actors(max_age=0))
    assert "https://f0.example/u" not in got and got["https://new.example/u"] == {"e" * 64}
    for i in range(150):                                    # every follower of d gone -> d has no followers
        _put(fake, state._FOLLOWER_PREFIX, "%s:%d" % ("d" * 64, i), {"actor": "x", "gone": True}, int(time.time()))
    assert asyncio.run(state.nostr_users_with_followers(max_age=0)) == frozenset()


def test_a_failed_cross_member_delta_is_not_nobody(fake, monkeypatch):
    _put(fake, state._FOLLOWER_PREFIX, "%s:1" % ("d" * 64), {"actor": "https://g.example/u"}, int(time.time()) - 10)
    state._nonlocal_cache["at"] = 0.0
    asyncio.run(state.nostr_users_with_followers(max_age=0))

    async def down(*a, **k):
        raise ConnectionError("relay down")
    monkeypatch.setattr(nostr_store, "list_docs", down)
    with pytest.raises(ConnectionError):
        asyncio.run(state.nostr_users_with_followers(max_age=0))
