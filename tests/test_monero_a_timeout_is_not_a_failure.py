"""A TIMEOUT WHILE SPENDING IS AN UNKNOWN, NOT A FAILURE — and it cost real money.

Measured on the live wallet: `transfer_split` was given the config's 8-second budget, which is a
READ budget. The call timed out, the client said "payment not sent", and the transaction had already
been broadcast. `get_transfers` afterwards showed TWO pending 0.001 XMR sends — from somebody who
had been told the first one failed and tried again.

Two separate defects, both fixed here:

1. A SPEND WAS GIVEN A READ'S BUDGET. Building and signing a transaction and handing it to the
   daemon is nothing like reading a balance. Spending methods now get 120s.

2. A TIMEOUT WAS WORDED AS A FAILURE. "Payment not sent" is a claim nobody can make about a request
   that never answered — the money may be gone. It now says so and sends the person to their
   history, and the UI does not offer an immediate retry.

Also corrected here: the busy message used to assert "it is still reading the chain", which was a
guess. A read can time out because the wallet is building a transaction or the daemon is loaded, and
telling a person with a fully synced wallet that it is still syncing is simply wrong — reported as
"how can it still be reading the chain". `sync_state` is what may claim that, because it measures.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from app.services import monero_user_wallets as users
from app.services import monero_wallet_service as svc

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static/js/client/monero-wallet.js").read_text(encoding="utf-8")


def cfg():
    return svc.WalletConfig(enabled=True, url="http://127.0.0.1:38083/json_rpc", username="u",
                            password="p", network="mainnet", transfer_cap_atomic=1,
                            daily_cap_atomic=1, timeout_seconds=8,
                            spend_ledger_path="data/x.sqlite3")


class Timeout(httpx.AsyncClient):
    async def post(self, *a, **k):
        raise httpx.ReadTimeout("slow")


@pytest.mark.parametrize("method", ["transfer", "transfer_split", "sweep_all", "sweep_single"])
def test_a_timed_out_spend_says_it_may_have_been_sent(method, monkeypatch):
    """THE ONE THAT COST MONEY."""
    monkeypatch.setattr(svc.httpx, "AsyncClient", Timeout)
    with pytest.raises(svc.WalletUnsure) as caught:
        asyncio.run(svc.MoneroWallet(cfg()).rpc(method))
    assert "may have been sent" in str(caught.value)
    assert "not sent" not in str(caught.value).replace("may have been sent", "")


@pytest.mark.parametrize("method", ["get_balance", "get_address", "get_transfers"])
def test_a_timed_out_read_is_just_busy(method, monkeypatch):
    """A read that times out moved no money, so it must not raise the alarm that one might have."""
    monkeypatch.setattr(svc.httpx, "AsyncClient", Timeout)
    with pytest.raises(svc.WalletError) as caught:
        asyncio.run(svc.MoneroWallet(cfg()).rpc(method))
    assert not isinstance(caught.value, svc.WalletUnsure)
    assert isinstance(caught.value, svc.WalletBusy)


def test_the_busy_message_does_not_claim_to_know_why():
    """"Still reading the chain" was a guess, and wrong for a synced wallet."""
    import inspect
    src = inspect.getsource(svc.MoneroWallet.rpc)
    assert "still reading the chain" not in src, (
        "the busy message asserts a cause again — a read can time out for several reasons")


def test_a_spend_gets_a_spends_budget():
    """8 seconds is a read budget. A transfer given it is what produced the double-send."""
    assert svc.MoneroWallet.SPEND_TIMEOUT >= 60
    assert "transfer_split" in svc.MoneroWallet.SPENDING
    assert "sweep_all" in svc.MoneroWallet.SPENDING
    assert "get_balance" not in svc.MoneroWallet.SPENDING


def test_the_user_wallets_follow_the_same_rules():
    """Users' money deserves the same care as the operator's."""
    assert users.UserWallets.SPEND_TIMEOUT >= 60
    assert users.UserWallets.SPENDING == svc.MoneroWallet.SPENDING


def test_the_user_wallet_also_reports_an_unsure_spend(monkeypatch):
    w = users.UserWallets()
    w.url, w.user, w.password = "http://x/json_rpc", "u", "p"
    monkeypatch.setattr(users.httpx, "AsyncClient", Timeout)
    with pytest.raises(svc.WalletUnsure):
        asyncio.run(w.rpc("transfer_split"))


def test_the_client_never_calls_an_unknown_a_failure():
    """The sheet said "payment not sent" over a payment that had gone out."""
    for marker in ("payment not sent", "tip not sent"):
        at = JS.index(marker)
        window = JS[max(0, at - 700):at + 200]
        assert "may have been sent" in window, (
            f"'{marker}' is still shown without distinguishing a timeout from a refusal")


def test_the_client_does_not_invite_an_immediate_retry_after_an_unknown():
    """Re-enabling the button is what turns one uncertain payment into two real ones."""
    assert JS.count("go.disabled = unsure") + JS.count("button.disabled = unsure") == 2, (
        "the send button is re-enabled after a timeout, inviting the double-send")
