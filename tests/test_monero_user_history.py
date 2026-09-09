"""A user can see their OWN Monero transaction history — and only their own.

Users could already see a balance and an address but had no way to answer "did that arrive?" or
"what did I send?"; only the node operator had /history. In a pooled wallet every user is an
ACCOUNT, so the whole feature turns on one property: the account index is resolved from the
caller's identity on the server, and there is no parameter through which a client could ask for
somebody else's.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import monero_user_wallets as mod
from app.services.monero_wallet_service import WalletError

from tests.test_monero_user_wallets import NPUB, OTHER, ADDR, pool


def _rows(major, txid, amount=1000000000000):
    return {"txid": txid, "amount": amount, "fee": 10, "confirmations": 3, "timestamp": 1700000000,
            "height": 42, "note": "", "double_spend_seen": False,
            "subaddr_index": {"major": major, "minor": 0}}


def wallet_with_transfers(transfers, capture=None):
    accounts = [{"account_index": 0, "label": "Primary account", "base_address": ADDR},
                {"account_index": 1, "label": mod.UserWallets()._label(NPUB), "base_address": "8" + "1" * 94},
                {"account_index": 2, "label": mod.UserWallets()._label(OTHER), "base_address": "8" + "2" * 94}]
    w = pool(accounts=accounts, capture=capture)
    inner = w.rpc

    async def rpc(method, params=None):
        if method == "get_transfers":
            if capture is not None:
                capture.append((method, params or {}))
            return transfers
        return await inner(method, params)
    w.rpc = rpc
    return w


def test_a_user_sees_their_own_transfers():
    w = wallet_with_transfers({"in": [_rows(1, "a" * 64)], "out": [_rows(1, "b" * 64)]})
    got = asyncio.run(w.history(NPUB))
    assert [r["txid"] for r in got["in"]] == ["a" * 64]
    assert [r["txid"] for r in got["out"]] == ["b" * 64]


def test_the_account_is_asked_for_by_index_and_it_is_the_callers():
    capture = []
    w = wallet_with_transfers({"in": [_rows(1, "a" * 64)]}, capture=capture)
    asyncio.run(w.history(NPUB))
    asked = [p for m, p in capture if m == "get_transfers"]
    assert asked and asked[0]["account_index"] == 1, asked


def test_another_users_rows_are_never_forwarded():
    """Belt and braces: even if the wallet answered with a foreign account, it must not reach them."""
    w = wallet_with_transfers({"in": [_rows(1, "a" * 64), _rows(2, "c" * 64)]})
    got = asyncio.run(w.history(NPUB))
    assert [r["txid"] for r in got["in"]] == ["a" * 64], "another account's transfer was returned"


def test_unconfirmed_money_is_visible_in_both_directions():
    """pending is OUTGOING unconfirmed, pool is INCOMING unconfirmed; a just-sent tip lives there."""
    w = wallet_with_transfers({"pending": [_rows(1, "d" * 64)], "pool": [_rows(1, "e" * 64)]})
    got = asyncio.run(w.history(NPUB))
    assert got["pending"][0]["txid"] == "d" * 64
    assert got["pool"][0]["txid"] == "e" * 64


def test_amounts_come_back_in_xmr_not_atomic_units():
    """A raw atomic amount shown to a person reads as 1,000,000,000,000 XMR."""
    w = wallet_with_transfers({"in": [_rows(1, "a" * 64, amount=1000000000000),
                                      _rows(1, "b" * 64, amount=1500000000)]})
    amounts = [r["amount"] for r in asyncio.run(w.history(NPUB))["in"]]
    assert amounts[0] == "1", amounts
    assert amounts[1].startswith("0.0015"), amounts


def test_a_user_with_no_wallet_is_told_so_rather_than_given_account_zero():
    """create=False matters: creating an account to show an empty history would mint a wallet for
    somebody merely looking, and defaulting to 0 would show them the NODE's transfers."""
    w = wallet_with_transfers({"in": [_rows(0, "n" * 64)]})
    with pytest.raises(WalletError):
        asyncio.run(w.history("npub1" + "y" * 58))


@pytest.mark.parametrize("limit", [0, 101, -1])
def test_a_silly_limit_is_refused(limit):
    w = wallet_with_transfers({"in": []})
    with pytest.raises(WalletError):
        asyncio.run(w.history(NPUB, limit=limit))


def test_the_limit_bounds_what_comes_back():
    rows = [_rows(1, format(i, "064x")) for i in range(20)]
    got = asyncio.run(wallet_with_transfers({"in": rows}).history(NPUB, limit=5))
    assert len(got["in"]) == 5


def test_the_endpoint_takes_no_account_parameter():
    """The only safe interface: a caller cannot name an account, so it cannot name another user's."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "app/routers/monero_user_wallet.py"
    body = src.read_text(encoding="utf-8")
    route = body[body.index('@router.get("/history")'):body.index("@router.post(\"/pay\")")]
    assert "account_index" not in route, "the route accepts an account index from the client"
    assert "_pubkey(user)" in route, "the account is not derived from the authenticated caller"
