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
from app.database import get_db
from app.models import ExodusWallet
from app.routers import auth


class _User:
    id = 4242
    nostr_npub = "f" * 64
    email = "wallet-test@example.invalid"


class _Query:
    def __init__(self, store): self._store = store
    def filter(self, *_a, **_k): return self
    def first(self): return self._store.get("row")


class _DB:
    """Just enough Session for these routes: one row, in memory."""
    def __init__(self): self.store = {}
    def query(self, *_a, **_k): return _Query(self.store)
    def add(self, row): self.store["row"] = row
    def commit(self): pass
    def rollback(self): pass


@pytest.fixture
def client(monkeypatch):
    db = _DB()
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


def _created(client):
    r = client.post("/api/wallet/exodus/create", json={})
    assert r.status_code == 200, r.text
    return r


def test_a_second_create_never_replaces_the_first_seed(client):
    """The expensive one. An overwrite here loses every coin behind the old seed, permanently."""
    _created(client)
    before = client._db.store["doc"]["pcai:exodus:wallet"]["seed"]
    again = client.post("/api/wallet/exodus/create", json={})
    assert again.status_code == 409, again.text
    assert client._db.store["doc"]["pcai:exodus:wallet"]["seed"] == before, "the seed was replaced"


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
    assert "pcai:exodus:wallet" not in client._db.store["doc"], "a bad phrase was stored anyway"


def test_importing_a_valid_phrase_restores_that_exact_wallet(client):
    vector = ("abandon abandon abandon abandon abandon abandon "
              "abandon abandon abandon abandon abandon about")
    assert client.post("/api/wallet/exodus/create", json={"mnemonic": vector}).status_code == 200
    got = client.get("/api/wallet/exodus/addresses").json()["addresses"]
    assert got["BTC"] == "1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA"
    assert client.post("/api/wallet/exodus/reveal").json()["mnemonic"] == vector


def test_revealing_marks_the_wallet_backed_up(client):
    """The nag exists because a seed nobody wrote down is one node failure from gone."""
    _created(client)
    assert client.get("/api/wallet/exodus/status").json()["backedUp"] is False
    client.post("/api/wallet/exodus/reveal")
    assert client._db.store["doc"]["pcai:exodus:wallet"]["backedUpAt"] > 0
    assert client.get("/api/wallet/exodus/status").json()["backedUp"] is True


def test_status_answers_even_with_no_wallet_and_says_who_holds_the_keys(client):
    body = client.get("/api/wallet/exodus/status").json()
    assert body["ok"] is True and body["exists"] is False
    assert "node holds the keys" in body["custody"].lower()
    symbols = {c["symbol"] for c in body["chains"]}
    # Monero is offered now, read from the node's own wallet rather than derived from this seed.
    assert {"BTC", "ETH", "XRP", "XMR"} <= symbols, symbols
    assert next(c for c in body["chains"] if c["symbol"] == "XMR")["kind"] == "node-wallet"


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
    body = {"symbol": "ETH", "to": "0x" + "11" * 20, "amount": "0.01"}
    body.update(kw)
    return client.post("/api/wallet/exodus/send", json=body)


def test_a_chain_that_cannot_send_says_so_instead_of_half_trying(client):
    _created(client)
    for sym in ("BTC", "LTC", "DOGE", "BCH", "SOL"):
        r = _send(client, symbol=sym, to="1" + "a" * 33)
        assert r.status_code == 501, (sym, r.status_code, r.text)
        assert "not supported yet" in r.text


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

def test_sending_monero_points_at_the_wallet_that_owns_the_coins(client):
    """Duplicating the spend path would mean two places moving the same coins under different
    limits — the Monero screen has caps and a spend ledger this one does not."""
    _created(client)
    r = client.post("/api/wallet/exodus/send",
                    json={"symbol": "XMR", "to": "4" + "a" * 94, "amount": "0.1"})
    assert r.status_code == 409, r.text
    assert "Monero Wallet screen" in r.text


def _monero_transport(monkeypatch, balance=1500000000000, unlocked=500000000000, failed=False):
    # Keep the actual exported singleton and its account lookup / balance conversion. Only the
    # wallet RPC boundary is stubbed: replacing the singleton with a factory hid a real outage.
    from app.services.monero_user_wallets import user_wallets
    from app.services.monero_wallet_service import WalletError
    calls = []
    async def rpc(method, params=None):
        calls.append((method, params))
        if failed:
            raise WalletError('The wallet service is unavailable')
        if method == 'get_accounts':
            return {'subaddress_accounts': [
                {'label': 'pc:' + 'a' * 64, 'account_index': 3, 'base_address': 'OTHER-USER'},
                {'label': 'pc:' + _User.nostr_npub, 'account_index': 7, 'base_address': '4' + 'b' * 94}]}
        assert method == 'get_balance' and params == {'account_index': 7}
        return {'balance': balance, 'unlocked_balance': unlocked}
    monkeypatch.setattr(user_wallets, 'enabled', lambda: True)
    monkeypatch.setattr(user_wallets, 'rpc', rpc)
    return calls


def test_an_unreachable_monero_wallet_is_unknown_not_zero(client, monkeypatch):
    _created(client)
    calls = _monero_transport(monkeypatch, failed=True)
    row = client.get('/api/wallet/exodus/balances').json()['balances']['XMR']
    assert calls == [('get_accounts', None)]
    assert row['known'] is False and row['amount'] is None and row['note']


def test_the_monero_row_reports_what_the_node_wallet_says(client, monkeypatch):
    _created(client)
    calls = _monero_transport(monkeypatch)
    row = client.get('/api/wallet/exodus/balances').json()['balances']['XMR']
    assert row['known'] is True and float(row['amount']) == 1.5
    assert float(row['spendable']) == 0.5 and 'locked' in row['note']
    assert calls[-1] == ('get_balance', {'account_index': 7})


def test_the_monero_address_is_the_node_wallets_not_one_derived_here(client, monkeypatch):
    _created(client)
    _monero_transport(monkeypatch, balance=0, unlocked=0)
    addrs = client.get('/api/wallet/exodus/addresses').json()['addresses']
    assert addrs['XMR'] == '4' + 'b' * 94
    assert addrs['XRP'].startswith('r') and addrs['BTC'].startswith('1')


def test_disabled_monero_is_reported_without_trying_the_network(client, monkeypatch):
    from app.services.monero_user_wallets import user_wallets
    _created(client)
    monkeypatch.setattr(user_wallets, 'enabled', lambda: False)
    rpc = AsyncMock(side_effect=AssertionError('disabled wallet must not connect'))
    monkeypatch.setattr(user_wallets, 'rpc', rpc)
    row = client.get('/api/wallet/exodus/balances').json()['balances']['XMR']
    assert not row['known'] and 'switched off' in row['note']
    rpc.assert_not_called()
