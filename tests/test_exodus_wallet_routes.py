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
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

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
    main.app.dependency_overrides[auth.get_current_user] = lambda: _User()
    main.app.dependency_overrides[get_db] = lambda: db
    with TestClient(main.app) as c:
        c._db = db
        yield c
    main.app.dependency_overrides.clear()


def _created(client):
    r = client.post("/api/wallet/exodus/create", json={})
    assert r.status_code == 200, r.text
    return r


def test_a_second_create_never_replaces_the_first_seed(client):
    """The expensive one. An overwrite here loses every coin behind the old seed, permanently."""
    _created(client)
    before = client._db.store["row"].seed_enc
    again = client.post("/api/wallet/exodus/create", json={})
    assert again.status_code == 409, again.text
    assert client._db.store["row"].seed_enc == before, "the seed was replaced"


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
    assert client._db.store.get("row") is None, "a bad phrase was stored anyway"


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
    assert isinstance(client._db.store["row"].backed_up_at, datetime)
    assert client.get("/api/wallet/exodus/status").json()["backedUp"] is True


def test_status_answers_even_with_no_wallet_and_says_who_holds_the_keys(client):
    body = client.get("/api/wallet/exodus/status").json()
    assert body["ok"] is True and body["exists"] is False
    assert "node holds the keys" in body["custody"].lower()
    assert {c["symbol"] for c in body["chains"]}
    assert "XMR" in body["excluded"]


def test_no_route_takes_a_user_or_wallet_id(client):
    """A pubkey parameter is an invitation to spend somebody else's money by typing theirs."""
    for route in main.app.routes:
        if str(getattr(route, "path", "")).startswith("/api/wallet/exodus"):
            names = set(getattr(route, "param_convertors", {}) or {})
            assert not names, f"{route.path} takes path parameters: {names}"
