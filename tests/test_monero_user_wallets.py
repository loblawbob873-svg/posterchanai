"""A MONERO WALLET FOR EVERY USER — one pooled wallet, one ACCOUNT each.

Asked for repeatedly: "I just want users to have a wallet to zap people easily."

WHY ACCOUNTS, NOT WALLETS. The first build gave each user their own wallet FILE behind a
`--wallet-dir` daemon. That daemon opens ONE wallet at a time, so every zap would have been
open + sync + transfer + close and concurrent zaps would queue — a bottleneck designed in on day
one. The user spotted it ("that sounds super bad"). One wallet with an account per user has no
open/close at all, one file to back up, and one thing to protect.

WHAT IS TRUE AND MUST STAY SAID: this is CUSTODIAL. The node holds these keys. It is a separate
wallet on a separate daemon and port from the operator's own, and the directory is backed up
encrypted off-box hourly — but the server can spend, and no design here changes that.

THE LABEL IS THE INDEX. A user's account is found by its label, which is their key. No second table
means nothing can disagree with the wallet about whose money is whose.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import monero_user_wallets as mod
from app.services.monero_wallet_service import WalletError

NPUB = "npub1" + "q" * 58
OTHER = "npub1" + "z" * 58
ADDR = "4" + "A" * 94
ADDR2 = "8" + "B" * 94


def pool(accounts=None, capture=None, reply=None):
    w = mod.UserWallets()
    w.url, w.user, w.password, w.network = "http://x/json_rpc", "u", "p", "mainnet"
    state = {"accounts": list(accounts or [{"account_index": 0, "label": "Primary account",
                                            "base_address": ADDR}])}

    async def rpc(method, params=None):
        if capture is not None:
            capture.append((method, params or {}))
        if method == "get_accounts":
            return {"subaddress_accounts": state["accounts"]}
        if method == "create_account":
            idx = len(state["accounts"])
            state["accounts"].append({"account_index": idx, "label": (params or {}).get("label"),
                                      "base_address": "8" + str(idx) * 94})
            return {"account_index": idx}
        if method == "get_balance":
            return {"balance": 5, "unlocked_balance": 3, "blocks_to_unlock": 0,
                    "per_subaddress": [{"num_unspent_outputs": 2}]}
        return reply if reply is not None else {"tx_hash_list": ["a" * 64], "amount_list": [1],
                                                "fee_list": [2]}
    w.rpc = rpc
    w._state = state
    return w


def test_a_user_gets_a_wallet_the_first_time_they_ask():
    w = pool()
    addr = asyncio.run(w.address(NPUB))
    assert addr and addr != ADDR, "the user was handed the pool's primary account"


def test_asking_twice_does_not_make_two_wallets():
    """THE ONE THAT LOSES MONEY IF IT IS WRONG. A second account would quietly become the one their
    address is published from, while anything sent in between sits in the first."""
    seen = []
    w = pool(capture=seen)
    a1 = asyncio.run(w.address(NPUB))
    a2 = asyncio.run(w.address(NPUB))
    assert a1 == a2
    assert len([c for c in seen if c[0] == "create_account"]) == 1


def test_concurrent_first_use_still_makes_one_wallet():
    """Two requests arriving together for a user who has none would both see 'none' and create
    one each. Serialised, with a re-check inside the lock."""
    seen = []
    w = pool(capture=seen)

    async def race():
        return await asyncio.gather(*[w.address(NPUB) for _ in range(6)])

    addrs = asyncio.run(race())
    assert len(set(addrs)) == 1, f"concurrent first use made {len(set(addrs))} wallets"
    assert len([c for c in seen if c[0] == "create_account"]) == 1


def test_two_users_get_two_wallets():
    w = pool()
    assert asyncio.run(w.address(NPUB)) != asyncio.run(w.address(OTHER))


def test_a_user_without_a_key_gets_no_wallet():
    """An empty key would become ONE SHARED WALLET for everybody who has not signed in with Nostr."""
    for bad in ("", None, "nope", "npub1!!!"):
        with pytest.raises(WalletError):
            asyncio.run(pool().address(bad))


def test_both_key_forms_are_accepted():
    """`User.nostr_npub` holds bech32 despite the model calling it a pubkey — checked against real
    rows. Assuming hex would have refused every existing user."""
    assert mod.UserWallets._label(NPUB).startswith("pc:npub1")
    assert mod.UserWallets._label("a" * 64) == "pc:" + "a" * 64


def test_paying_many_people_is_one_transaction():
    seen = []
    w = pool(capture=seen)
    asyncio.run(w.pay(NPUB, [(ADDR, 100)] * 10))
    calls = [c for c in seen if c[0] == "transfer_split"]
    assert len(calls) == 1 and len(calls[0][1]["destinations"]) == 10


def test_more_than_a_transaction_holds_is_batched():
    seen = []
    w = pool(capture=seen)
    asyncio.run(w.pay(NPUB, [(ADDR, 100)] * 40))
    calls = [c for c in seen if c[0] == "transfer_split"]
    assert [len(c[1]["destinations"]) for c in calls] == [15, 15, 10]


def test_a_payment_draws_only_on_that_users_account():
    """The whole safety property. Paying from the wrong account_index spends somebody else's money."""
    seen = []
    w = pool(capture=seen)
    asyncio.run(w.address(OTHER))                      # so the target user is not index 1
    idx = asyncio.run(w.account(NPUB))["account_index"]
    seen.clear()
    asyncio.run(w.pay(NPUB, [(ADDR, 100)]))
    assert [c for c in seen if c[0] == "transfer_split"][0][1]["account_index"] == idx


def test_a_bad_address_stops_the_whole_batch():
    """Fourteen good payments must not go out before the fifteenth is found to be invalid."""
    seen = []
    w = pool(capture=seen)
    with pytest.raises(WalletError):
        asyncio.run(w.pay(NPUB, [(ADDR, 100), ("5" + "C" * 94, 100)]))
    assert not [c for c in seen if c[0] == "transfer_split"]


@pytest.mark.parametrize("amount", [0, -5, None, True, "10"])
def test_a_nonsense_amount_sends_nothing(amount):
    seen = []
    w = pool(capture=seen)
    with pytest.raises(WalletError):
        asyncio.run(w.pay(NPUB, [(ADDR, amount)]))
    assert not [c for c in seen if c[0] == "transfer_split"]


def test_withdraw_sweeps_that_users_account_to_their_address():
    """CUSTODY WITHOUT A WAY OUT IS NOT A WALLET, IT IS AN IOU. This exists from the first commit."""
    seen = []
    w = pool(capture=seen)
    asyncio.run(w.address(NPUB))
    seen.clear()
    asyncio.run(w.withdraw(NPUB, ADDR2))
    method, params = [c for c in seen if c[0] == "sweep_all"][0]
    assert params["address"] == ADDR2
    assert params["account_index"] == asyncio.run(w.account(NPUB))["account_index"]


def test_withdraw_refuses_an_address_for_another_network():
    with pytest.raises(WalletError):
        asyncio.run(pool().withdraw(NPUB, "5" + "C" * 94))


def test_withdraw_does_not_create_a_wallet():
    """Sweeping an account that was just conjured would report success having moved nothing."""
    with pytest.raises(WalletError):
        asyncio.run(pool().withdraw(NPUB, ADDR2))


def test_balance_reports_what_the_user_can_actually_spend():
    got = asyncio.run(pool().balance(NPUB))
    assert set(got) >= {"address", "balance", "unlocked_balance", "blocks_to_unlock", "outputs"}
    assert got["unlocked_balance"] == "0.000000000003"


def test_an_unconfigured_node_says_so_rather_than_guessing():
    w = mod.UserWallets()
    w.url = w.user = w.password = ""
    assert w.enabled() is False
    with pytest.raises(WalletError):
        asyncio.run(w.rpc("get_accounts"))
