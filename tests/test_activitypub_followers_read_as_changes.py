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
