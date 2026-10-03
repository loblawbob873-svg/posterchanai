"""A relay-blocked account is blocked for EVERY kind, and a block the relay never saved is never "done".

Reported 2026-10-02: "i blocked him at the relay, removed his nip05, and he just opened a git issue" /
"he was able to play games and DM me after being blocked". Two bugs, both silent:

1. The block was never applied. `/client/block` stored the list through the background settings
   writer and told the relay to reload AT ONCE; the relay re-reads the list from its own event store,
   so the reload (18:00:45) read the OLD 489-key list, the write landed later (18:01:04, after two
   handshake timeouts) and nothing reloaded it. The admin saw "blocked" and the npub in Admin -> Relay.
2. Even an applied block only lived in `is_member` at the WoT gate, which the branches that accept
   from ANY author by design (git issues/patches/PRs on a repo announced here, webxdc realtime, ...)
   never reach.

These drive the SHIPPED `_on_event` with real signed events and the shipped `set_blocked`.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import relay_blocklist, settings_store
from app.services.nostr import nostr_service
from app.services.nostr.event import build_event
from app.services.nostr import bip340
from tests.test_grasp_pr_kinds_accepted import OWNER, REPO, FakeGate, deliver, relay

BLOCKED_SK = bytes.fromhex("22" * 32)
BLOCKED_PK = bip340.pubkey_from_seckey(BLOCKED_SK).hex()
STRANGER_SK = bytes.fromhex("33" * 32)


class BlockingGate(FakeGate):
    def is_blocked(self, pk):
        return pk == BLOCKED_PK


def _relay():
    s = relay()
    s.gate = BlockingGate()
    return s


def _ev(kind, sk=BLOCKED_SK):
    tags = {
        1617: [["a", f"30617:{OWNER}:{REPO}"]],
        1618: [["a", f"30617:{OWNER}:{REPO}"], ["c", "b" * 40]],
        1621: [["a", f"30617:{OWNER}:{REPO}"], ["subject", "Too complexity"], ["p", OWNER]],
        1630: [["a", f"30617:{OWNER}:{REPO}"], ["e", "c" * 64]],
        20932: [["i", "game-identifier"]],
        30617: [["d", "his-repo"]],
        4: [["p", OWNER]],
        1: [],
    }[kind]
    return build_event(sk, kind, "x", tags)


@pytest.mark.parametrize("kind", [1621, 1617, 1618, 1630, 20932, 30617, 4, 1])
def test_a_blocked_author_is_refused_for_every_kind(kind):
    """1621 is the issue that was actually filed; 20932 is a game's realtime channel. Before the fix the
    git kinds and 20932 came back OK=true, because their branches never ask about the author."""
    s = _relay()
    ok = deliver(s, _ev(kind))
    assert ok[2] is False and "blocked" in ok[3], ok
    assert not s.store.added, "a blocked author's event must not be stored"


def test_a_stranger_who_is_not_blocked_can_still_open_an_issue():
    """The guard against 'fixed it by closing the repo': GRASP-01 makes issues from anyone a MUST."""
    s = _relay()
    ok = deliver(s, _ev(1621, sk=STRANGER_SK))
    assert ok[2] is True, ok


# ----- the block has to be SAVED before the relay is told to reload it -----

@pytest.fixture
def blocklist(monkeypatch):
    vals = {relay_blocklist.KEY: ""}
    order = []
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))

    def put(k, v, **kw):
        vals[k] = v
    monkeypatch.setattr(settings_store, "put", put)
    monkeypatch.setattr(settings_store, "restore_cached",
                        lambda k, prev: vals.__setitem__(k, prev) if prev is not None else vals.pop(k, None))
    relay_doc = {}
    state = {"fail": False}

    async def write_through(db, changes):
        if state["fail"]:
            return 0
        order.append("write")
        relay_doc.update(changes)
        return len(changes)
    monkeypatch.setattr(settings_store, "write_through", write_through)
    import app.services.nostr_relay.thread as thread

    def reload():
        # What the relay does on reload-blocks: re-read the list from ITS store, not our cache.
        order.append(("reload", relay_doc.get(relay_blocklist.KEY, "")))
        return {}
    monkeypatch.setattr(thread, "trigger_block_reload", reload)
    import app.services.blossom_service as bs
    monkeypatch.setattr(bs, "_operator_pubkeys", lambda db: set())
    return vals, order, state


def test_the_relay_reloads_a_list_that_already_holds_the_block(blocklist):
    """The 18:00:45 failure: the reload ran before the write, so the relay re-read a list without him."""
    vals, order, _ = blocklist
    r = asyncio.run(relay_blocklist.set_blocked(None, BLOCKED_PK, True))
    assert r["ok"] is True
    assert order[0] == "write", order
    kind, seen = order[1]
    assert kind == "reload" and nostr_service.npub_of(BLOCKED_PK) in seen, \
        "the relay reloaded a block list that did not contain the account"


def test_a_block_the_relay_did_not_save_is_an_error_not_a_success(blocklist):
    """`/client/block` answered 200 while both relay writes were timing out."""
    vals, order, state = blocklist
    state["fail"] = True
    r = asyncio.run(relay_blocklist.set_blocked(None, BLOCKED_PK, True))
    assert r["ok"] is False and r["status"] == 503
    assert vals.get(relay_blocklist.KEY, "") == "", "the cache must not claim a block the relay never stored"
    assert not order, "nothing may be reloaded on the strength of a write that did not land"


def test_the_admin_textarea_saves_the_block_lists_durably():
    """Admin -> Relay's textarea Save had the same race: put() (background) then reload at once."""
    from app.routers import admin
    assert relay_blocklist.KEY in admin._BLOCK_DURABLE
    for k in ("nostr_relay_blocked_words", "nostr_relay_blocked_langs", "nostr_relay_blocked_relays"):
        assert k in admin._BLOCK_DURABLE
