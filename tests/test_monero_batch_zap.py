"""ZAPPING MANY PEOPLE AT ONCE IS ONE TRANSACTION, NOT ONE PER PERSON.

Asked for directly: "make sure I can zap a lot at once to many users. same for every user."

A Monero wallet spends whole OUTPUTS and locks the change for 10 blocks, so paying people one at a
time means one payment and then roughly twenty minutes of nothing — measured on the live wallet,
which held a single unspent output while five tips went out one at a time.

The fix is not more outputs. Monero takes a list of destinations natively, so ten tips are ONE
transaction with one fee and no wait between them. Measured against the real wallet from that same
single output, with `do_not_relay` so nothing was spent:

     5 destinations -> 1 transaction, fee 0.0000676 XMR
    10 destinations -> 1 transaction, fee 0.0001163 XMR
    16 destinations -> REFUSED, "tx not possible"

16 fails because the outputs in one transaction are capped and the CHANGE takes a slot. Hence
batching at 15, and `transfer_split` rather than `transfer` so the daemon may split a batch further
when the inputs require it instead of failing outright.

`account_index` is what makes this work for every user rather than only the operator: in the pooled
wallet each user is an account, and a payment draws only on that account's own outputs.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import monero_wallet_service as svc

ADDR = "4" + "A" * 94
OTHER = "8" + "B" * 94


def wallet(capture=None, reply=None):
    w = svc.MoneroWallet.__new__(svc.MoneroWallet)
    w.config = svc.WalletConfig(enabled=True, url="http://127.0.0.1:38083/json_rpc", username="u",
                                password="p", network="mainnet", transfer_cap_atomic=10**12,
                                daily_cap_atomic=10**12, timeout_seconds=2,
                                spend_ledger_path="data/x.sqlite3")

    async def rpc(method, params=None):
        if capture is not None:
            capture.append((method, params or {}))
        return reply if reply is not None else {
            "tx_hash_list": ["a" * 64], "amount_list": [1], "fee_list": [2]}
    w.rpc = rpc
    return w


def test_ten_recipients_are_one_transaction():
    """THE POINT. Ten separate transfers would be one payment and a twenty-minute wait."""
    seen = []
    asyncio.run(wallet(capture=seen).transfer_many([(ADDR, 1000)] * 10))
    calls = [c for c in seen if c[0] == "transfer_split"]
    assert len(calls) == 1, f"ten recipients produced {len(calls)} transactions"
    assert len(calls[0][1]["destinations"]) == 10


def test_more_than_the_cap_is_batched_not_refused():
    """16 destinations is refused by the daemon — the change needs an output slot. A caller asking
    to pay 40 people must not get an error; it must get batches."""
    seen = []
    got = asyncio.run(wallet(capture=seen).transfer_many([(ADDR, 1000)] * 40))
    calls = [c for c in seen if c[0] == "transfer_split"]
    assert len(calls) == 3, f"40 recipients should batch into 3 transactions, got {len(calls)}"
    assert [len(c[1]["destinations"]) for c in calls] == [15, 15, 10]
    assert got["recipients"] == 40


def test_every_batch_stays_under_the_measured_limit():
    seen = []
    asyncio.run(wallet(capture=seen).transfer_many([(ADDR, 1)] * 100))
    for _, params in [c for c in seen if c[0] == "transfer_split"]:
        assert len(params["destinations"]) <= 15, "a batch exceeds what the daemon accepts"


def test_it_pays_from_the_users_own_account():
    """THE 'same for every user' HALF. In the pooled wallet each user is an account, and a payment
    must draw only on that account's outputs — not on somebody else's, and not on the operator's."""
    seen = []
    asyncio.run(wallet(capture=seen).transfer_many([(ADDR, 1000)], account_index=7))
    assert seen[-1][1]["account_index"] == 7


def test_the_default_account_is_the_operators():
    seen = []
    asyncio.run(wallet(capture=seen).transfer_many([(ADDR, 1000)]))
    assert seen[-1][1]["account_index"] == 0


def test_transfer_split_is_used_so_a_batch_can_still_be_split():
    """`transfer` fails when the inputs cannot cover a batch in one transaction; `transfer_split`
    splits it instead. On a wallet made of one output that is the difference between working and
    'tx not possible'."""
    seen = []
    asyncio.run(wallet(capture=seen).transfer_many([(ADDR, 1000)] * 3))
    assert seen[-1][0] == "transfer_split"


def test_every_address_is_validated_for_this_network():
    """One bad address in a batch of fifteen must not send fourteen good payments and fail after."""
    with pytest.raises(svc.WalletError):
        asyncio.run(wallet().transfer_many([(ADDR, 1000), ("5" + "C" * 94, 1000)]))


@pytest.mark.parametrize("amount", [0, -1, None, True, "100"])
def test_a_nonsense_amount_is_refused_before_anything_is_sent(amount):
    seen = []
    with pytest.raises(svc.WalletError):
        asyncio.run(wallet(capture=seen).transfer_many([(ADDR, amount)]))
    assert not [c for c in seen if c[0] == "transfer_split"], "it started paying anyway"


def test_an_empty_batch_is_refused():
    with pytest.raises(svc.WalletError):
        asyncio.run(wallet().transfer_many([]))


def test_the_totals_are_summed_across_batches():
    """A caller needs one answer for what the whole thing cost, not per batch."""
    w = wallet(reply={"tx_hash_list": ["b" * 64], "amount_list": [5], "fee_list": [3]})
    got = asyncio.run(w.transfer_many([(ADDR, 1)] * 30))
    assert len(got["tx_hash_list"]) == 2
    assert got["recipients"] == 30
    assert got["batches"] == [15, 15]
