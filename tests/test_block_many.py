"""Trace -> "Block N selected": a follow-back ring is blocked with ONE signature, ONE write, ONE relay reload.

Reported 2026-10-08 after tracing an AI reply bot: each of ten engagement-farm accounts was let in by 7-21
follow-back bots inside the web of trust, and blocking them one by one from each profile was the only way --
"need a easy way to block the accounts in trace, sometimes it can be a lot". Runs the SHIPPED route and
service with real signed admin proofs (the store fixture is test_relay_blocklist's).
"""
import asyncio
import json

from app.services import relay_blocklist
from app.services.nostr import bip340
from tests.test_relay_blocklist import ADMIN_PK, NSFW, OTHER, _DB, _proof, store  # noqa: F401  (fixture)

RING = [bip340.pubkey_from_seckey((0xF00 + i).to_bytes(32, "big")).hex() for i in range(5)]


def _block_many(targets, proof):
    from app.routers import client
    return asyncio.run(client.block_many(client.BlockManyReq(targets=targets, auth=proof), _DB()))


def test_one_signed_request_blocks_the_whole_selection_with_one_reload(store):
    vals, reloads = store
    r = _block_many(RING, _proof("block-many", [["action", "block-many"]] + [["p", p] for p in RING]))
    body = json.loads(r.body)
    assert r.status_code == 200 and body["ok"] and body["changed"] == len(RING), body
    assert set(RING) <= set(relay_blocklist.blocked_hex())
    assert len(reloads) == 1, f"one selection reloaded the relay {len(reloads)} times"


def test_a_signature_cannot_be_stretched_to_a_key_it_never_named(store):
    proof = _proof("block-many", [["action", "block-many"]] + [["p", p] for p in RING[:4]])
    r = _block_many(RING, proof)
    assert r.status_code == 403
    assert not set(RING) & set(relay_blocklist.blocked_hex()), "a key the admin never signed for was blocked"


def test_a_proof_for_another_action_is_refused(store):
    r = _block_many(RING[:1], _proof("block", [["action", "block"], ["p", RING[0]]]))
    assert r.status_code == 403 and RING[0] not in relay_blocklist.blocked_hex()


def test_the_nodes_own_key_is_skipped_and_said_so_while_the_rest_are_blocked(store):
    targets = [RING[0], ADMIN_PK]
    r = _block_many(targets, _proof("block-many", [["p", p] for p in targets]))
    body = json.loads(r.body)
    assert r.status_code == 200 and body["refused"] == [ADMIN_PK], body
    assert RING[0] in relay_blocklist.blocked_hex() and ADMIN_PK not in relay_blocklist.blocked_hex()


def test_the_single_block_still_answers_as_before(store):
    assert asyncio.run(relay_blocklist.set_blocked(None, RING[1], True)) == {"ok": True, "blocked": True, "count": 3}
    assert asyncio.run(relay_blocklist.set_blocked(None, ADMIN_PK, True))["ok"] is False
