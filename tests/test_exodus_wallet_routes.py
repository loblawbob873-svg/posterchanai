"""The wallet's routes, driven against the real FastAPI app with a stubbed session.

WHAT THESE EXIST TO CATCH, in order of how much they cost when wrong:

  1. A SECOND `create` REPLACING SOMEBODY'S SEED. "Create" is exactly the button a person presses
     twice when a page looks slow, and the row it would overwrite is the only copy of the key to
     their coins. Nothing else in this app has a failure mode that expensive.
  2. The phrase leaking out of a route nobody asked. `/status` and `/addresses` are drawn on load;
     if either carries the mnemonic it has been shoulder-surfed by whoever walked past.
  3. "No wallet" and "wallet is locked" being answered the same way, which is what makes an app
     offer to generate a new seed over the top of an existing one.
"""
import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI
from unittest.mock import AsyncMock

import app.main as main
from app.database import get_db, Base
from app.models import ExodusWallet, ExodusWalletRecord, User
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.routers import auth


class _User:
    id = 4242
    nostr_npub = "f" * 64
    email = "wallet-test@example.invalid"


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("EXODUS_TRANSFER_DIR", str(tmp_path / "transfers"))
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[User.__table__, ExodusWallet.__table__, ExodusWalletRecord.__table__])
    db = Session(engine)
    db.add(User(id=4242, username='wallet-fixture', password_hash='unused')); db.commit()
    db.store = {}
    # A fixed storage key: these tests are about the routes, not about key management.
    monkeypatch.setattr("app.services.nostr_store.user_storage_seckey",
                        lambda _db, _u: bytes(range(32)), raising=False)
    # The relay stands in as one in-memory document, which is what the vault treats it as. Reads
    # and writes go through the real vault code; only the transport is stubbed.
    doc_store = {}

    async def _get_doc(_port, d_tag, **_kw):
        return doc_store.get(d_tag)

    async def _put_doc(_port, _sk, d_tag, data, **_kw):
        doc_store[d_tag] = data
        return True

    monkeypatch.setattr("app.services.nostr_store.get_doc", _get_doc, raising=False)
    monkeypatch.setattr("app.services.nostr_store.put_doc", _put_doc, raising=False)
    db.store["doc"] = doc_store
    monkeypatch.setattr('app.services.exodus_bitcoin_discovery.balance', AsyncMock(return_value={'known':False,'amount':None,'note':'Discovering'}))
    app = FastAPI()
    from app.routers.exodus_wallet import router
    app.include_router(router)
    monkeypatch.setattr("app.services.exodus_chain_service.balances", AsyncMock(return_value={}))
    app.dependency_overrides[auth.get_current_user] = lambda: _User()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        c._db = db
        yield c
    app.dependency_overrides.clear()
    db.close(); engine.dispose()


def _created(client):
    r = client.post("/api/wallet/exodus/create", json={})
    assert r.status_code == 200, r.text
    return r


def test_a_second_create_never_replaces_the_first_seed(client):
    """The expensive one. An overwrite here loses every coin behind the old seed, permanently."""
    _created(client)
    before = client._db.store["doc"]["pcai:exodus:collection:default"]["seed"]
    again = client.post("/api/wallet/exodus/create", json={})
    assert again.status_code == 409, again.text
    assert client._db.store["doc"]["pcai:exodus:collection:default"]["seed"] == before, "the seed was replaced"


def test_create_does_not_return_the_phrase(client):
    """It is shown once, through /reveal, because the person has to ask."""
    body = _created(client).json()
    assert "mnemonic" not in body and "seed" not in body


def test_status_and_addresses_never_carry_the_phrase(client):
    _created(client)
    phrase = client.post("/api/wallet/exodus/reveal").json()["mnemonic"]
    for path in ("/api/wallet/exodus/status", "/api/wallet/exodus/addresses"):
        text = client.get(path).text
        assert phrase not in text, f"{path} leaked the recovery phrase"
        for word in phrase.split()[:3]:
            assert f'"{word}"' not in text


def test_reveal_is_a_post_not_a_get(client):
    """A GET is linkable, prefetchable, and lands in browser history."""
    _created(client)
    assert client.get("/api/wallet/exodus/reveal").status_code in (404, 405)
    assert client.post("/api/wallet/exodus/reveal").status_code == 200


def test_no_wallet_is_404_and_stays_distinguishable_from_locked(client):
    assert client.get("/api/wallet/exodus/addresses").status_code == 404
    assert client.post("/api/wallet/exodus/reveal").status_code == 404
    assert client.get("/api/wallet/exodus/status").json()["exists"] is False


def test_an_unreadable_seed_is_503_not_404(client, monkeypatch):
    """503 means 'there IS a wallet and it will not open'. Answering 404 invites a fresh create."""
    _created(client)
    monkeypatch.setattr("app.services.nostr_store.user_storage_seckey",
                        lambda _db, _u: bytes(reversed(range(32))), raising=False)
    r = client.get("/api/wallet/exodus/addresses")
    assert r.status_code == 503, r.text


def test_importing_a_phrase_with_a_wrong_word_is_refused(client):
    """It would derive a valid, different wallet: an empty balance for an account that is not
    theirs, with nothing on screen to say why."""
    bad = ("abandon " * 11 + "zoo").strip()
    r = client.post("/api/wallet/exodus/create", json={"mnemonic": bad})
    assert r.status_code == 400, r.text
    assert "pcai:exodus:collection:default" not in client._db.store["doc"], "a bad phrase was stored anyway"


def test_importing_a_valid_phrase_restores_that_exact_wallet(client):
    vector = ("abandon abandon abandon abandon abandon abandon "
              "abandon abandon abandon abandon abandon about")
    assert client.post("/api/wallet/exodus/create", json={"mnemonic": vector}).status_code == 200
    got = client.get("/api/wallet/exodus/addresses").json()["addresses"]
    assert got["BTC"] == "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"
    assert client.post("/api/wallet/exodus/reveal").json()["mnemonic"] == vector


def test_revealing_marks_the_wallet_backed_up(client):
    """The nag exists because a seed nobody wrote down is one node failure from gone."""
    _created(client)
    assert client.get("/api/wallet/exodus/status").json()["backedUp"] is False
    client.post("/api/wallet/exodus/reveal")
    assert client._db.store["doc"]["pcai:exodus:collection:default"]["backedUpAt"] > 0
    assert client.get("/api/wallet/exodus/status").json()["backedUp"] is True


def test_status_answers_even_with_no_wallet_and_says_who_holds_the_keys(client):
    body = client.get("/api/wallet/exodus/status").json()
    assert body["ok"] is True and body["exists"] is False
    assert "server-managed" in body["custody"].lower()
    assert "recovery backup" in body["custody"].lower()
    symbols = {c["symbol"] for c in body["chains"]}
    # Monero is independently derived from this wallet, without the built-in tipping wallet.
    assert {"BTC", "ETH", "XRP", "XMR"} <= symbols, symbols
    assert next(c for c in body["chains"] if c["symbol"] == "XMR")["kind"] == "monero"


def test_no_route_takes_a_user_or_wallet_id(client):
    """A pubkey parameter is an invitation to spend somebody else's money by typing theirs."""
    for route in main.app.routes:
        if str(getattr(route, "path", "")).startswith("/api/wallet/exodus"):
            names = set(getattr(route, "param_convertors", {}) or {})
            assert not names, f"{route.path} takes path parameters: {names}"


# ── sending ───────────────────────────────────────────────────────────────────────────────────
#
# The three answers a send can give, and why the third exists at all:
#   200  it is on the chain
#   400  it was refused before anything was signed
#   202  IT MAY BE ON THE CHAIN. Not an error status, deliberately — a client that sees 4xx or 5xx
#        offers a retry, and a retry here is a second real payment. This codebase already made that
#        exact mistake once on Monero, where a 20s client timeout over a live transfer reported
#        "payment not sent".

def _send(client, **kw):
    body = {"requestId": "01"*16, "symbol": "ETH", "to": "0x" + "11" * 20, "amount": "0.01"}
    body.update(kw)
    return client.post("/api/wallet/exodus/send", json=body)


def test_a_chain_that_cannot_send_says_so_instead_of_half_trying(client):
    _created(client)
    for sym in ("BTC", "LTC", "DOGE", "BCH"):
        r = _send(client, symbol=sym, to="1" + "a" * 33)
        assert r.status_code == 501, (sym, r.status_code, r.text)
        assert "not available" in r.text


@pytest.mark.parametrize('symbol', ['SOL', 'XRP'])
def test_native_account_send_uses_selected_wallet_keys_and_preserves_pending_retry(client, monkeypatch, symbol):
    from app.services import exodus_account_send as A, exodus_derivation as D
    _created(client)
    phrase = client.post('/api/wallet/exodus/reveal').json()['mnemonic']
    captured = []
    async def send(**kwargs):
        captured.append(kwargs)
        assert kwargs['private_key'] == D.private_key(phrase, symbol, format=D.EXODUS)
        assert kwargs['from_address'] == D.address(phrase, symbol, format=D.EXODUS)
        assert kwargs['units'] == (10000000 if symbol == 'SOL' else 10000)
        await kwargs['before_broadcast']('fixture-hash', 7)
        return {'hash': 'fixture-hash', 'pending': True}
    monkeypatch.setattr(A, 'send_solana' if symbol == 'SOL' else 'send_xrp', send)
    first = _send(client, symbol=symbol, destinationTag=123 if symbol == 'XRP' else None)
    assert first.status_code == 200 and first.json()['pending'] is True
    assert _send(client, symbol=symbol, destinationTag=123 if symbol == 'XRP' else None).json() == first.json()
    blocked = _send(client, symbol=symbol, requestId='02'*16)
    assert blocked.status_code == 202 and blocked.json()['unsure'] is True
    assert len(captured) == 1
    if symbol == 'XRP': assert captured[0]['destination_tag'] == 123


@pytest.mark.parametrize('tag', [-1, 4294967296, True, '123', 1.5])
def test_destination_tag_validation_rejects_coercion_before_signing(client, tag):
    assert _send(client, symbol='XRP', destinationTag=tag).status_code == 422


def test_an_amount_with_too_much_precision_is_refused_before_signing(client):
    _created(client)
    r = _send(client, amount="0." + "0" * 18 + "1")   # 19 decimals; ETH has 18
    assert r.status_code == 400 and "decimal places" in r.text


def test_nonsense_amounts_never_reach_a_signature(client):
    _created(client)
    for bad in ("abc", "-1", "0", "1e9"):
        assert _send(client, amount=bad).status_code == 400, bad


def test_the_cap_is_enforced_on_the_server(client, monkeypatch):
    """A cap the page enforces is a cap anybody can skip by calling the endpoint."""
    _created(client)
    monkeypatch.setattr("app.services.settings_store.all_settings",
                        lambda: {"exodus_cap_eth": "0.05"}, raising=False)
    over = _send(client, amount="0.06")
    assert over.status_code == 400 and "limit" in over.text


def test_a_misconfigured_cap_refuses_rather_than_becoming_no_cap(client, monkeypatch):
    """The failure mode that matters: an unparseable ceiling must not silently mean 'unlimited'."""
    _created(client)
    monkeypatch.setattr("app.services.settings_store.all_settings",
                        lambda: {"exodus_cap_eth": "lots"}, raising=False)
    r = _send(client, amount="0.01")
    assert r.status_code == 503, r.text
    assert "not a valid amount" in r.text


def test_an_uncertain_broadcast_is_202_and_never_an_error(client, monkeypatch):
    """202 so no client offers a retry. The message must name the nonce to look for."""
    _created(client)
    from app.services import exodus_send_service as S

    async def unsure(**_kw):
        raise S.SendUnsure("signed and sent; check for nonce 7 before sending again")
    monkeypatch.setattr(S, "send_evm", unsure, raising=False)
    r = _send(client)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["unsure"] is True and body["ok"] is False
    assert "nonce" in body["msg"]


def test_a_send_with_no_wallet_is_404(client):
    assert _send(client).status_code == 404


# ── Monero: the wallet that already exists, not a second one ──────────────────────────────────

def test_monero_address_belongs_to_this_seed_and_never_calls_the_builtin_wallet(client, monkeypatch):
    from app.services import exodus_wallet_service as W
    from app.services.monero_user_wallets import user_wallets
    forbidden = AsyncMock(side_effect=AssertionError('built-in wallet must not be used'))
    monkeypatch.setattr(user_wallets, 'rpc', forbidden)
    _created(client)
    phrase = client.post('/api/wallet/exodus/reveal').json()['mnemonic']
    addresses = client.get('/api/wallet/exodus/addresses').json()['addresses']
    assert addresses['XMR'] == W.monero_keys(phrase).PrimaryAddress()
    assert not forbidden.called


def test_independent_monero_is_unknown_when_unconfigured(client, monkeypatch):
    monkeypatch.delenv('EXODUS_MONERO_DAEMON', raising=False)
    _created(client)
    row = client.get('/api/wallet/exodus/balances').json()['balances']['XMR']
    assert row['known'] is False and row['amount'] is None and row['note']
    assert row['address'] == client.get('/api/wallet/exodus/addresses').json()['addresses']['XMR']


def test_monero_recovery_exports_only_on_explicit_post(client):
    from bip_utils import MoneroMnemonicDecoder, Monero
    _created(client)
    assert client.get('/api/wallet/exodus/reveal-monero').status_code == 405
    words = client.post('/api/wallet/exodus/reveal-monero').json()['mnemonic']
    assert len(words.split()) == 25
    keys = Monero.FromPrivateSpendKey(MoneroMnemonicDecoder().Decode(words))
    assert keys.PrimaryAddress() == client.get('/api/wallet/exodus/addresses').json()['addresses']['XMR']
