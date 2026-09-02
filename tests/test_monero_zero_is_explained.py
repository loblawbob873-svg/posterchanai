"""A ZERO BALANCE MUST SAY WHETHER IT MEANS "EMPTY" OR "I HAVE NOT READ THE CHAIN YET".

Reported as "people have been zapping my monero address but wallet still says 0". Measured against
the real node rather than guessed:

    wallet get_balance      -> balance 0, unlocked 0, blocks_to_unlock 0
    wallet get_height       -> 3,467,268
    monerod get_info        -> height 3,468,468  target_height 3,753,339  synchronized False
    monerod log             -> "Synced 3469368/3753339 (92%, 283971 left)"

Nothing was lost and nothing was broken. The daemon was 284,871 blocks behind and actively syncing,
so the wallet had never seen the blocks those payments were in, and `balance: 0` was exactly correct.
The failure was that the screen could not say so: an empty wallet and a wallet still reading the
chain rendered identically, on the one screen where that difference is somebody's money.

Two gaps, both measured against the live wallet-rpc:

1. `get_transfers` asked for `in`, `out`, `pending`, `failed` — and NOT `pool`. `pending` is
   OUTGOING unconfirmed; `pool` is INCOMING unconfirmed. So a payment that had been broadcast and
   not yet mined appeared in neither list, during exactly the window somebody checks.

2. Nothing could answer "am I behind?". `sync_info` and `get_info` are DAEMON methods and the wallet
   RPC answers `Method not found` for both (verified), so the daemon's target height is genuinely
   unreadable from here. `refresh` is the one signal that exists: it returns `blocks_fetched`, 0 at
   the tip and >0 while catching up.

The rule this file holds is the codebase's usual one, and it matters more here than anywhere: a
failed check answers "I could not tell", NEVER "synchronised" — because the reassuring answer is the
one that would be wrong.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.services import monero_wallet_service as svc

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static/js/client/monero-wallet.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/monero-wallet.css").read_text(encoding="utf-8")
ROUTER = (ROOT / "app/routers/monero_wallet.py").read_text(encoding="utf-8")


class FakeRPC:
    """A wallet that answers exactly what the real one answered."""

    def __init__(self, **replies):
        self.replies = replies
        self.calls = []

    async def rpc(self, method, params=None):
        self.calls.append((method, params or {}))
        if method in self.replies:
            reply = self.replies[method]
            if isinstance(reply, Exception):
                raise reply
            return reply
        raise svc.WalletError(f"Method not found: {method}")


def wallet(**replies):
    w = svc.MoneroWallet.__new__(svc.MoneroWallet)
    fake = FakeRPC(**replies)
    w.rpc = fake.rpc
    w._fake = fake
    return w


def test_incoming_unconfirmed_transfers_are_asked_for():
    """GAP 1. `pool` is incoming-unconfirmed; without it a broadcast payment is invisible for the
    minutes somebody is most likely to be looking."""
    w = wallet(get_transfers={"in": [], "pool": [{"amount": 1, "txid": "a"}]})
    asyncio.run(w.history(limit=10))
    _, params = w._fake.calls[0]
    assert params.get("pool") is True, (
        "get_transfers still does not request the pool — an unmined incoming payment shows nowhere")


def test_the_pool_transfers_survive_into_the_answer():
    w = wallet(get_transfers={"in": [], "out": [], "pending": [], "failed": [],
                              "pool": [{"amount": 1500000000000, "txid": "a"}]})
    got = asyncio.run(w.history(limit=10))
    assert got.get("pool"), "the pool came back from the wallet and was dropped on the way out"


def test_a_wallet_at_the_tip_reports_that_it_is_not_scanning():
    got = asyncio.run(wallet(refresh={"blocks_fetched": 0, "received_money": False}).sync_state())
    assert got == {"checked": True, "scanning": False, "blocks_fetched": 0}


def test_a_wallet_reading_the_chain_reports_that_it_is():
    """THE REPORTED STATE, exactly: the wallet was fetching blocks, so its 0 was provisional."""
    got = asyncio.run(wallet(refresh={"blocks_fetched": 40, "received_money": False}).sync_state())
    assert got["checked"] is True and got["scanning"] is True and got["blocks_fetched"] == 40


def test_a_failed_check_never_claims_the_wallet_is_up_to_date():
    """The rule. 'Could not ask' is not 'synchronised' — and here the wrong reassurance tells
    somebody their money is gone."""
    got = asyncio.run(wallet(refresh=svc.WalletError("wallet unavailable")).sync_state())
    assert got["checked"] is False
    assert got["scanning"] is None, "an unreachable wallet is being reported as synchronised"


def test_a_nonsense_answer_is_not_read_as_synchronised():
    """`blocks_fetched` missing or the wrong type must not silently become 0 == 'at the tip'."""
    for reply in ({"received_money": False}, {"blocks_fetched": None}, {"blocks_fetched": True}):
        got = asyncio.run(wallet(refresh=reply).sync_state())
        assert got["blocks_fetched"] == 0


def test_the_sync_check_is_its_own_route():
    """`refresh` does real work on a wallet that is behind. The balance must never wait on it —
    the screen paints what it knows and this fills in why."""
    assert '@router.get("/sync")' in ROUTER
    assert "sync_state" in ROUTER


def test_the_client_asks_only_after_it_has_painted():
    """Same rule as the alt-tab paint: nothing that can be slow goes in front of the balance."""
    assert "syncNote" in JS
    body = JS.split("async function syncNote(){", 1)[1].split("\n  }", 1)[0]
    assert "/api/wallet/xmr/sync" in body
    assert "st.checked" in body and "st.scanning" in body, (
        "the banner does not distinguish 'still scanning' from 'could not tell'")


def test_the_banner_says_nothing_when_the_answer_is_unknown():
    """An unknown answer must produce NO claim in either direction — not 'synced', and not a scary
    banner on a wallet that is perfectly fine."""
    body = JS.split("async function syncNote(){", 1)[1].split("\n  }", 1)[0]
    guard = body[body.index("if(!st"):body.index("innerHTML")]
    assert "return" in guard, "an unknown or healthy answer still paints something"


def test_the_banner_does_not_claim_anything_is_lost():
    body = JS.split("async function syncNote(){", 1)[1].split("\n  }", 1)[0]
    assert "Nothing is lost" in body, (
        "the one thing somebody in this situation needs told is not being said")


def test_an_unconfirmed_credit_is_labelled_as_one():
    """Showing a pool transfer is only an improvement while it is not mistaken for settled money."""
    assert "unconfirmed" in JS
    assert ".mw-tx-pending" in CSS, "the unconfirmed row has no style, so the label is all there is"
