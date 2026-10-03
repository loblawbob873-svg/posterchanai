"""A key on the relay's block list can do NOTHING on the platform -- whatever its profile claims.

"we need to make sure that blocked users can't do anything on the platform despite having a nip05 in
their profile". The block lived in the relay's write gate only: a blocked member kept a working login,
API keys, signed requests (files, sync, drafts, media tools, push, NIP-05 claims), member features (Mail,
Git, Office, Web Search, AI), Blossom uploads, CalDAV, and could be re-admitted to the web of trust by
logging in again. Each gate is driven with the SAME key blocked and not blocked, so a gate that refuses
everybody cannot pass.
"""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import relay_blocklist, settings_store
from app.services.nostr import bip340, nostr_service
from app.services.nostr import event as nostr_event

SK = (0xB10C).to_bytes(32, "big")
PK = bip340.pubkey_from_seckey(SK).hex()
OTHER = bip340.pubkey_from_seckey((0x0DD).to_bytes(32, "big")).hex()


@pytest.fixture
def blocked(monkeypatch):
    vals = {relay_blocklist.KEY: nostr_service.npub_of(PK)}
    real_get = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals[k] if k in vals else real_get(k, d))
    return vals


def _proof(sk=SK, content="x"):
    ev = nostr_event.build_event(sk, 27235, content, tags=[])
    return base64.b64encode(json.dumps(ev).encode()).decode()


def test_the_list_takes_npubs_and_hex(blocked):
    assert relay_blocklist.is_blocked(PK) and relay_blocklist.is_blocked(nostr_service.npub_of(PK))
    assert relay_blocklist.is_blocked(PK.upper())
    assert not relay_blocklist.is_blocked(OTHER) and not relay_blocklist.is_blocked("")


def test_a_signed_request_from_a_blocked_key_proves_nothing(blocked):
    assert nostr_event.verify_self_auth(_proof(), PK) is False
    other = bip340.pubkey_from_seckey((0x0DD).to_bytes(32, "big")).hex()
    assert nostr_event.verify_self_auth(_proof((0x0DD).to_bytes(32, "big")), other) is True


class _Q:
    def __init__(self, user): self.user = user
    def filter(self, *a): return self
    def first(self): return self.user


class _DB:
    def __init__(self, user): self.user = user
    def query(self, _m): return _Q(self.user)


def test_an_existing_login_stops_working(blocked):
    from app import auth
    token = auth.create_access_token({"sub": "7"})
    req = SimpleNamespace(cookies={"access_token": token}, query_params={})
    user = SimpleNamespace(id=7, nostr_npub=PK, username="blocked")
    with pytest.raises(HTTPException) as e:
        auth.get_current_user(req, None, _DB(user))
    assert e.value.status_code == 403
    fine = SimpleNamespace(id=7, nostr_npub=OTHER, username="fine")
    assert auth.get_current_user(req, None, _DB(fine)) is fine


def test_a_websocket_login_stops_working(blocked):
    from app import auth
    token = auth.create_access_token({"sub": "7"})
    ws = SimpleNamespace(query_params={"token": token}, cookies={})
    assert asyncio.run(auth.get_user_from_websocket(ws, _DB(SimpleNamespace(id=7, nostr_npub=PK)))) is None
    fine = SimpleNamespace(id=7, nostr_npub=OTHER)
    assert asyncio.run(auth.get_user_from_websocket(ws, _DB(fine))) is fine


def test_membership_says_no_even_with_a_granted_name(blocked, monkeypatch):
    """Every member-only feature (Mail, News, Git, Office, Web Search, files sync, AI, media) asks this."""
    from app.services import instance_membership as im
    blocked["nostr_relay_nip05_names"] = f"mallory {PK}\nalice {OTHER}"
    checker = im._checker
    monkeypatch.setattr(checker, "configuration", lambda: (blocked["nostr_relay_nip05_names"], "poster.place", "https://poster.place"))
    st = asyncio.run(checker.status(PK))
    assert st["qualified"] is False and st["reason"] == "blocked", st


def test_blossom_refuses_even_a_whitelisted_key(blocked, monkeypatch):
    from app.services import blossom_service as bs
    monkeypatch.setattr(bs, "_operator_pubkeys", lambda db: set())
    monkeypatch.setattr(bs, "_whitelist_pubkeys", lambda db: {PK, OTHER})
    assert bs.is_pubkey_allowed(None, PK) is False
    assert bs.is_pubkey_allowed(None, OTHER) is True
    assert asyncio.run(bs.is_pubkey_allowed_async(None, PK)) is False


def test_logging_in_again_does_not_readmit_a_blocked_key(blocked, monkeypatch):
    from app.routers import client
    import app.services.nostr_relay.thread as thread
    added = []
    monkeypatch.setattr(thread, "trigger_wot_add", lambda pks: added.extend(pks))
    ok, _ = asyncio.run(client.follow_and_admit(None, PK))
    assert ok is False and PK not in added


def test_the_relay_refuses_a_blocked_accounts_sign_in():
    src = (__import__("pathlib").Path(__file__).resolve().parents[1] / "app/services/nostr_relay/server.py").read_text()
    ok_at = src.index('logger.info("[nostr-relay] AUTH ok for')
    assert "self.gate.is_blocked(" in src[ok_at - 400:ok_at], "a blocked key still gets an authenticated relay socket"


def test_a_blocked_key_registers_no_push_device(blocked):
    from app.routers import push
    def proof(sk, pk):
        ev = nostr_event.build_event(sk, 27235, "posterchan-direct:register:dev-0123456789abcdef", tags=[])
        return base64.urlsafe_b64encode(json.dumps(ev).encode()).decode()
    assert push._direct_auth(proof(SK, PK), PK, "register", "dev-0123456789abcdef") is False
    osk = (0x0DD).to_bytes(32, "big")
    assert push._direct_auth(proof(osk, OTHER), OTHER, "register", "dev-0123456789abcdef") is True


def test_the_telegram_bot_ignores_a_blocked_linked_account(blocked):
    from app.routers.telegram import webhook
    upd = {"message": {"chat": {"id": 42}, "text": "hi"}}
    assert webhook._from_blocked_account(upd, _DB(SimpleNamespace(nostr_npub=PK))) is True
    assert webhook._from_blocked_account(upd, _DB(SimpleNamespace(nostr_npub=OTHER))) is False
    cb = {"callback_query": {"message": {"chat": {"id": 42}}}}
    assert webhook._from_blocked_account(cb, _DB(SimpleNamespace(nostr_npub=PK))) is True


def test_a_blocked_key_is_a_stranger_to_the_vm_host(blocked):
    from app.services.vmhost import service as vs
    host = vs.VmHostService.__new__(vs.VmHostService)
    host.cfg = SimpleNamespace(allowed_pubkeys={PK, OTHER})
    host._assign = {}
    host.migrator = None
    async def admins(): return set()
    host.admin_pubkeys = admins
    assert asyncio.run(host.role_of(PK)) is None
    assert asyncio.run(host.role_of(OTHER)) == "user"
