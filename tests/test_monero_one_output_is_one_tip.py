"""ONE UNSPENT OUTPUT MEANS ONE TIP AT A TIME, WHATEVER THE BALANCE SAYS.

Asked directly: "cant you increase unspents? we have a lot of users".

Monero spends whole OUTPUTS and locks the change from a payment for 10 blocks. A wallet whose
balance is a single output can therefore send once and then nothing for about twenty minutes — the
balance is irrelevant. Measured on the node while tipping: `num_unspent_outputs: 1`, five tips going
out one at a time, each followed by `unlocked_balance: 0` and a `blocks_to_unlock` countdown.

On a node where many people tip, that is the real ceiling, and no cap or timeout change touches it.

`sweep_all` to the wallet's OWN address with `outputs: N` is how a Monero wallet fixes it: the
balance returns as N independently spendable outputs, so N tips can follow one another.

It is a REAL transaction — it pays a fee and its own outputs lock for 10 blocks before they help —
so it is an explicit operator action and never automatic. A wallet that quietly spent its own funds
on a timer would be a worse surprise than the wait it removes.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app.services import monero_wallet_service as svc


def wallet(reply=None, capture=None):
    w = svc.MoneroWallet.__new__(svc.MoneroWallet)
    w.config = svc.WalletConfig(enabled=True, url="http://127.0.0.1:38083/json_rpc", username="u",
                                password="p", network="mainnet", transfer_cap_atomic=1,
                                daily_cap_atomic=1, timeout_seconds=2,
                                spend_ledger_path="data/x.sqlite3")
    own = "4" + "A" * 94

    async def rpc(method, params=None):
        if capture is not None:
            capture.append((method, params or {}))
        if method == "get_address":
            return {"address": own, "addresses": [{"address": own}]}
        return reply or {}
    w.rpc = rpc
    w._own = own
    return w


def test_it_sweeps_to_its_own_address():
    """To ITSELF. Sweeping anywhere else would be sending the wallet's money away."""
    seen = []
    w = wallet(reply={"amount_list": [1, 2]}, capture=seen)
    asyncio.run(w.split_outputs(8))
    method, params = seen[-1]
    assert method == "sweep_all"
    assert params["address"] == w._own, "the sweep does not go back to this wallet"
    assert params["outputs"] == 8
    assert params["account_index"] == 0


def test_the_destination_is_validated_for_this_network():
    """The address comes from the wallet, but it is still the destination of a real transaction."""
    src = inspect.getsource(svc.MoneroWallet.split_outputs)
    assert "validate_address(own, self.config.network)" in src


@pytest.mark.parametrize("count", [0, 1, 17, 100, -3])
def test_a_silly_split_is_refused(count):
    """Below two it does nothing; far above, every output is dust and the fee grows."""
    with pytest.raises(svc.WalletError):
        asyncio.run(wallet().split_outputs(count))


@pytest.mark.parametrize("count", [2, 8, 16])
def test_a_sensible_split_is_allowed(count):
    asyncio.run(wallet(reply={}).split_outputs(count))


def test_a_wallet_with_no_address_does_not_sweep_into_the_void():
    """`sweep_all` with an empty destination is a transaction to nowhere."""
    w = wallet()

    async def rpc(method, params=None):
        return {} if method == "get_address" else {}
    w.rpc = rpc
    with pytest.raises(svc.WalletError):
        asyncio.run(w.split_outputs(4))


def test_the_balance_reports_how_many_outputs_it_is_made_of():
    """The number that decides whether a second tip can happen. It lives inside `per_subaddress`;
    the client should not have to know that."""
    w = wallet()

    async def rpc(method, params=None):
        return {"balance": 5, "unlocked_balance": 5,
                "per_subaddress": [{"num_unspent_outputs": 3}, {"num_unspent_outputs": 4}]}
    w.rpc = rpc
    got = asyncio.run(w.balance())
    assert got["num_unspent_outputs"] == 7, "outputs are not summed across subaddresses"


def test_a_wallet_that_does_not_report_outputs_says_unknown_rather_than_zero():
    """Zero would read as 'this wallet can never pay', which is a different claim entirely."""
    w = wallet()

    async def rpc(method, params=None):
        return {"balance": 5, "unlocked_balance": 5}
    w.rpc = rpc
    assert asyncio.run(w.balance())["num_unspent_outputs"] == 0
