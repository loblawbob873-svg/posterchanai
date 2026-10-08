"""Admin -> Identities: "Remove, unfollow & block" retires accounts that signed up and never did anything.

"for the users that signed up and never posted thing, we need a way to unfollow them and make sure they can't
come back later and DDOS the place" (2026-10-08). The rules, each a test: the BLOCK goes first (it is what keeps
them out -- signup and login refuse a blocked key); every name the key holds goes; the operator's contact list is
REWRITTEN from a read that answered, minus exactly those keys (a replaceable list written from a failed read is
the follows-wipe); admins and the node's own keys are never retired.
"""
import asyncio
from unittest import mock

import pytest

from app.routers import admin, client
from app.services import nip05_registry, relay_blocklist, settings_store
from app.services.nostr import bip340, nostr_service
from app.services.nostr import event as nostr_event

OP_SK = (0x0F0F).to_bytes(32, "big")
OP_PK = bip340.pubkey_from_seckey(OP_SK).hex()
GHOST, LURKER, KEEP, ADMIN = (bip340.pubkey_from_seckey((0x500 + i).to_bytes(32, "big")).hex() for i in range(4))


@pytest.fixture
def world(monkeypatch):
    vals = {nip05_registry.KEY: "\n".join([
        f"sadsadsadsa {GHOST}", f"ghost2 {nostr_service.npub_of(GHOST)}", f"lurker {LURKER}",
        f"keeper {KEEP}", f"boss {ADMIN}"]), relay_blocklist.KEY: ""}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(settings_store, "put", lambda k, v, **kw: vals.__setitem__(k, v))
    monkeypatch.setattr(settings_store, "is_hydrated", lambda: True)

    async def write_through(db, changes):
        vals.update(changes)
        return len(changes)
    monkeypatch.setattr(settings_store, "write_through", write_through)
    import app.services.nostr_relay.thread as thread
    monkeypatch.setattr(thread, "trigger_block_reload", lambda: {})
    monkeypatch.setattr(thread, "trigger_nip05_reload", lambda: {}, raising=False)
    import app.services.blossom_service as bs
    monkeypatch.setattr(bs, "_operator_pubkeys", lambda db: {OP_PK})
    calls = []

    async def revoke(db, pks):
        calls.append(("revoke", sorted(pks)))
        return {"accounts": len(pks)}
    import app.services.relay_access_policy as rap
    monkeypatch.setattr(rap, "revoke_identities", revoke)
    k3 = nostr_event.build_event(OP_SK, 3, "", tags=[["p", GHOST], ["p", LURKER], ["p", KEEP], ["p", "e" * 64]],
                                 created_at=1791000000)
    state = {"k3": k3, "answered": True, "published": []}

    async def fetch(port, pk, timeout=8.0):
        return (state["k3"], True) if state["answered"] else (None, False)

    async def publish(port, ev, timeout=8.0):
        calls.append(("publish", ev["kind"]))
        state["published"].append(ev)
        return True, ""
    monkeypatch.setattr(client, "_fetch_kind3_strict", fetch)
    monkeypatch.setattr(client, "_publish_to_relay", publish)
    monkeypatch.setattr(client, "_operator", lambda db: mock.Mock(nostr_nsec=OP_SK.hex()))
    monkeypatch.setattr(client, "_setting", lambda db, k, d=None: d)
    orig_block = relay_blocklist.set_blocked_many

    async def block(db, pks, on):
        calls.append(("block", sorted(pks)))
        return await orig_block(db, pks, on)
    monkeypatch.setattr(relay_blocklist, "set_blocked_many", block)
    return vals, state, calls


class _DB:
    def query(self, _m): return self
    def filter(self, *a): return self
    def all(self): return [mock.Mock(nostr_npub=nostr_service.npub_of(ADMIN))]


def _retire(pks):
    return asyncio.run(admin.relay_identities_retire(admin.RelayIdentitiesRetireReq(pubkeys=pks), db=_DB(), admin=None))


def test_block_first_then_every_name_then_the_operator_unfollows_exactly_those_keys(world):
    vals, state, calls = world
    out = _retire([GHOST, LURKER])
    assert [c[0] for c in calls][:1] == ["block"], f"the block must go first: {calls}"
    assert set(relay_blocklist.blocked_hex()) == {GHOST, LURKER}
    assert sorted(out["names_removed"]) == ["ghost2", "lurker", "sadsadsadsa"], "a key kept one of its names"
    reg = vals[nip05_registry.KEY]
    assert "keeper" in reg and "boss" in reg and "lurker" not in reg and "sadsadsadsa" not in reg
    assert out["unfollowed"] == 2 and not out.get("unfollow_error")
    new = state["published"][-1]
    assert new["kind"] == 3 and new["created_at"] > state["k3"]["created_at"]
    assert [t for t in new["tags"] if t[0] == "p"] == [["p", KEEP], ["p", "e" * 64]], "unfollow removed more than asked"


def test_an_unanswered_read_writes_no_follow_list(world):
    vals, state, calls = world
    state["answered"] = False
    out = _retire([GHOST])
    assert GHOST in relay_blocklist.blocked_hex(), "the block must not wait on the follow list"
    assert out["unfollowed"] == 0 and "could not read" in out["unfollow_error"]
    assert not state["published"], "a follow list was written from a read that never answered"


def test_admins_and_the_nodes_own_key_are_never_retired(world):
    vals, state, calls = world
    out = _retire([ADMIN, OP_PK, GHOST])
    assert out["skipped_admins"] == [ADMIN] and out["refused_operator_keys"] == [OP_PK]
    assert set(relay_blocklist.blocked_hex()) == {GHOST}
    assert "boss" in vals[nip05_registry.KEY]


def test_a_retired_key_cannot_sign_up_or_log_in_again(world):
    _retire([GHOST])
    ok, msg = asyncio.run(client.follow_and_admit(None, GHOST))
    assert ok is False and "blocked" in msg
