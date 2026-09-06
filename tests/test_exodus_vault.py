"""THE WALLET LIVES IN A REPLACEABLE NOSTR DOCUMENT, AND THIS IS WHAT STOPS THAT LOSING IT.

A kind-30078 document is REPLACED by whatever is written next. This codebase has recorded the same
accident more than once: an unreachable relay answers an empty read, the empty read is written back,
and the document is gone. For a mute list that costs a re-follow. For a wallet seed it costs every
coin behind it, permanently, with nothing anywhere able to reconstruct it.

So the store has three rules and each one is tested here by making the failure happen:

  1. Every read is STRICT -- an unreachable relay RAISES rather than answering "no wallet", because
     a caller that cannot tell those apart offers to generate a second seed over the first.
  2. No write happens without a read first, and a write that would replace the seed -- with a
     different one or with nothing -- is refused inside the store, not left to the caller.
  3. The ciphertext is mirrored into a row that is NEVER read unless the document is absent, because
     "the relay lost it" and "there was never a wallet" look identical from outside and one of those
     two answers is somebody's money.
"""
import asyncio

import pytest

from app.services import exodus_vault as V
from app.services import exodus_wallet_service as W

VECTOR = ("abandon abandon abandon abandon abandon abandon "
          "abandon abandon abandon abandon abandon about")


class _User:
    id = 77
    nostr_npub = "a" * 64


class _Row:
    def __init__(self): self.seed_enc = None; self.label = None; self.address_index = 0
    backed_up_at = None
    created_at = None


class _DB:
    def __init__(self): self.row = None
    def query(self, *_a): return self
    def filter(self, *_a, **_k): return self
    def first(self): return self.row
    def add(self, r): self.row = r
    def commit(self): pass


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture
def vault(monkeypatch):
    docs, state = {}, {"reachable": True, "writes": 0}

    async def get_doc(_port, d_tag, **_kw):
        if not state["reachable"]:
            raise ConnectionError("relay unreachable")
        return docs.get(d_tag)

    async def put_doc(_port, _sk, d_tag, data, **_kw):
        if not state["reachable"]:
            return False
        state["writes"] += 1
        docs[d_tag] = data
        return True

    monkeypatch.setattr("app.services.nostr_store.get_doc", get_doc, raising=False)
    monkeypatch.setattr("app.services.nostr_store.put_doc", put_doc, raising=False)
    monkeypatch.setattr("app.services.nostr_store.user_storage_seckey",
                        lambda _db, _u: bytes(range(32)), raising=False)
    return {"docs": docs, "state": state, "db": _DB(), "user": _User()}


# ── rule 1: an unreachable relay is never "no wallet" ─────────────────────────────────────────
def test_an_unreachable_relay_raises_instead_of_answering_no_wallet(vault):
    vault["state"]["reachable"] = False
    with pytest.raises(V.VaultUnavailable):
        _run(V.load(vault["db"], vault["user"]))


def test_a_genuinely_absent_document_is_none_not_an_error(vault):
    assert _run(V.load(vault["db"], vault["user"])) is None


def test_creating_over_an_unreachable_relay_is_refused(vault):
    """The exact shape of the disaster: a create that treats 'cannot read' as 'nothing there'
    replaces a live seed."""
    _run(V.save_new(vault["db"], vault["user"], VECTOR, None))
    vault["state"]["reachable"] = False
    with pytest.raises(V.VaultUnavailable):
        _run(V.save_new(vault["db"], vault["user"], W.new_mnemonic(), None))


# ── rule 2: nothing overwrites a seed ─────────────────────────────────────────────────────────
def test_a_second_create_is_refused(vault):
    _run(V.save_new(vault["db"], vault["user"], VECTOR, None))
    before = vault["docs"][V.D_TAG]["seed"]
    with pytest.raises(W.WalletError):
        _run(V.save_new(vault["db"], vault["user"], W.new_mnemonic(), None))
    assert vault["docs"][V.D_TAG]["seed"] == before


def test_a_metadata_change_carries_the_seed_forward_verbatim(vault):
    """A label edit, a rotated receive index, marking it backed up -- none of them may be able to
    lose a wallet, so `update` cannot be passed a seed even by accident."""
    _run(V.save_new(vault["db"], vault["user"], VECTOR, None))
    before = vault["docs"][V.D_TAG]["seed"]
    _run(V.update(vault["db"], vault["user"], label="rent", addressIndex=3, backedUpAt=99))
    doc = vault["docs"][V.D_TAG]
    assert doc["seed"] == before
    assert doc["label"] == "rent" and doc["addressIndex"] == 3 and doc["backedUpAt"] == 99
    assert V.mnemonic_of(vault["db"], vault["user"], doc) == VECTOR


def test_a_seedless_write_is_refused_inside_the_store(vault):
    """The wipe itself. Guarded here rather than in the caller, because every caller is one bug
    away from being the one that does it."""
    with pytest.raises(W.WalletError):
        _run(V._publish(vault["db"], vault["user"], {"label": "x", "addressIndex": 0}))


def test_a_document_that_lost_its_seed_is_unavailable_not_empty(vault):
    """Something wrote over it. Answering 'no wallet' here is what invites a second one on top."""
    _run(V.save_new(vault["db"], vault["user"], VECTOR, None))
    vault["docs"][V.D_TAG] = {"label": "x", "addressIndex": 0}
    with pytest.raises(V.VaultUnavailable):
        _run(V.load(vault["db"], vault["user"]))


# ── rule 3: the backup, and only when the document is gone ────────────────────────────────────
def test_a_lost_document_is_restored_from_the_encrypted_backup(vault):
    """'The relay lost it' and 'there was never a wallet' look identical from outside, and one of
    those two answers is somebody's money."""
    _run(V.save_new(vault["db"], vault["user"], VECTOR, None))
    vault["docs"].clear()
    doc = _run(V.load(vault["db"], vault["user"]))
    assert doc is not None
    assert V.mnemonic_of(vault["db"], vault["user"], doc) == VECTOR
    assert V.D_TAG in vault["docs"], "the restore did not republish the document"


def test_the_backup_is_ciphertext_not_words(vault):
    _run(V.save_new(vault["db"], vault["user"], VECTOR, None))
    blob = bytes(vault["db"].row.seed_enc)
    assert VECTOR.encode() not in blob
    for word in ("abandon", "about"):
        assert word.encode() not in blob


def test_the_relay_never_holds_a_readable_mnemonic(vault):
    import json
    _run(V.save_new(vault["db"], vault["user"], VECTOR, None))
    blob = json.dumps(vault["docs"][V.D_TAG])
    assert "abandon" not in blob and "about" not in blob


def test_a_write_the_relay_refused_is_an_error_not_a_silent_loss(vault):
    _run(V.save_new(vault["db"], vault["user"], VECTOR, None))
    vault["state"]["reachable"] = True
    async def refuse(*_a, **_k): return False
    import app.services.nostr_store as store
    store.put_doc = refuse
    with pytest.raises(V.VaultUnavailable):
        _run(V.update(vault["db"], vault["user"], label="x"))
