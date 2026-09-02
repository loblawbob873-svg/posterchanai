"""THE TIPPING WALLET'S MAINTAINER HAS TO COUNT WHAT CAN BE SPENT, NOT WHAT IS UNSPENT.

Reported, repeatedly and finally as "WHY CAN'T YOU MAKE MONERO STABLE!", after the maintainer was
already written and running on a timer.

`scripts/monero_keep_outputs.py` splits the wallet into several outputs so that tips can follow one
another instead of waiting out Monero's 10-block change lock. Its first version fired only when
`get_balance`'s `num_unspent_outputs` was exactly 1. That field counts outputs that are UNSPENT,
which includes every output still inside its lock — so it is not a count of what the wallet can
spend. The live wallet, minutes after the user hit the wait again:

    outputs total   : 3          <- the old rule saw this and stood down
    outputs UNLOCKED: 1          <- what could actually be spent
    0.049000000 XMR  unlocked=False   (a deposit that had just arrived)
    0.000330158 XMR  unlocked=False   (change from the previous tip)
    0.000052183 XMR  unlocked=True

    journal: "outputs=3: nothing to do (only a single output blocks the next tip)"

One spendable output is exactly the state the maintainer exists to prevent: the next tip consumes
it, the change locks, and the wallet has nothing. Worse, `outs == 1` can only be reached AFTER the
wallet is already blocked, so the old rule could never act in time.

The fix counts unlocked outputs and acts at a LOW-WATER MARK rather than topping up to the target —
topping up would re-split after every payment and pay a fee each time, which is what the original
narrowness was rightly guarding against.

`decide()` is a pure function so these run with no wallet, no daemon and no network.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/monero_keep_outputs.py"

spec = importlib.util.spec_from_file_location("keep_outputs", SCRIPT)
keep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keep)

XMR = 10 ** 12


def test_the_exact_live_state_the_old_rule_called_healthy():
    """THE BUG, with the numbers off the wallet. Three unspent outputs, one spendable, a funded
    wallet — and the old rule did nothing because it was looking at the 3."""
    go, outputs, why = keep.decide(spendable_count=1, unlocked_atomic=int(0.049 * XMR))
    assert go, f"still standing down with one spendable output: {why}"
    assert outputs >= 2


def test_a_single_spendable_output_is_always_acted_on():
    """One spendable output means the next tip blocks the one after it. That is the condition."""
    go, _, _ = keep.decide(spendable_count=1, unlocked_atomic=5 * XMR)
    assert go


def test_zero_spendable_outputs_is_acted_on_when_there_is_anything_to_split():
    go, _, _ = keep.decide(spendable_count=0, unlocked_atomic=5 * XMR)
    assert go


def test_a_healthy_wallet_is_left_alone():
    """The guard that stops fee churn: a wallet with plenty of spendable outputs must not be
    re-split after every payment, which is why this is a low-water mark and not a target."""
    go, _, why = keep.decide(spendable_count=8, unlocked_atomic=5 * XMR)
    assert not go, why
    go, _, _ = keep.decide(spendable_count=keep.LOW_WATER, unlocked_atomic=5 * XMR)
    assert not go, "splitting at the low-water mark itself re-splits too eagerly"


def test_it_does_not_split_dust():
    """A split costs a fee. Below the floor it is all cost, so waiting is correct — and this is the
    state the wallet was ACTUALLY in when measured, with 0.0000522 XMR unlocked."""
    go, _, why = keep.decide(spendable_count=1, unlocked_atomic=int(0.0000522 * XMR))
    assert not go
    assert "waiting" in why


def test_it_never_mints_outputs_too_small_to_spend():
    """With little unlocked, N tiny outputs are worth less than a few usable ones — each one costs
    an input in some later transaction."""
    go, outputs, _ = keep.decide(spendable_count=1, unlocked_atomic=int(0.003 * XMR))
    assert go
    assert outputs >= 2
    assert outputs * keep.MIN_OUT_ATOMIC <= int(0.003 * XMR) or outputs == 2


def test_a_big_balance_splits_to_the_full_target():
    go, outputs, _ = keep.decide(spendable_count=1, unlocked_atomic=50 * XMR)
    assert go and outputs == keep.TARGET


def test_the_low_water_mark_leaves_room_before_the_wallet_blocks():
    """It has to fire while the wallet can still send, not once it cannot. `outs == 1` was already
    too late by construction."""
    assert keep.LOW_WATER >= 2, "acting only at one spendable output is the original bug"


def test_the_unspent_count_is_not_what_drives_the_decision():
    """Names the regression directly: nothing in the decision may key on num_unspent_outputs."""
    source = SCRIPT.read_text(encoding="utf-8")
    decide_src = source[source.index("def decide("):source.index("def rpc(")]
    assert "num_unspent_outputs" not in decide_src, (
        "the decision reads num_unspent_outputs again — that counts locked outputs and is what "
        "made the maintainer stand down with one spendable output")


def test_spendable_reads_the_per_output_unlocked_flag():
    """`incoming_transfers` is the only call that reports the lock per output."""
    source = SCRIPT.read_text(encoding="utf-8")
    body = source[source.index("def spendable("):source.index("def main(")]
    assert "incoming_transfers" in body and '"unlocked"' in body


def test_a_wallet_that_cannot_report_the_flag_is_not_assumed_healthy():
    """The fallback must not read 'no flag' as 'everything is spendable' — that is the old bug
    wearing a different hat. It degrades to the unlocked BALANCE, which every version reports."""
    source = SCRIPT.read_text(encoding="utf-8")
    body = source[source.index("    count, measured = spendable()"):source.index("    go, outputs")]
    assert "1 if unlocked > 0 else 0" in body, (
        "an unmeasurable wallet is being treated as having many spendable outputs")


@pytest.mark.parametrize("count,unlocked", [(0, 0), (1, 0), (0, 1), (99, 99 * XMR)])
def test_the_decision_never_raises(count, unlocked):
    keep.decide(spendable_count=count, unlocked_atomic=unlocked)


# ── The whole script against a fake wallet ──────────────────────────────────────────────────────
#
# `decide()` did not exist when the bug shipped, so a test of `decide()` alone would not have caught
# it: the defect was in WHAT WAS COUNTED before any decision was made. These drive `main()` with the
# RPC replaced, reproducing the live wallet exactly, and assert on whether a sweep actually happens.

class FakeWallet:
    """The measured live state: three unspent outputs, one of them spendable."""

    def __init__(self, transfers, unlocked_atomic, num_unspent=None):
        self.transfers = transfers
        self.unlocked = unlocked_atomic
        self.num_unspent = len(transfers) if num_unspent is None else num_unspent
        self.swept = []

    def __call__(self, method, params=None):
        if method == "get_balance":
            return {"result": {"balance": sum(t["amount"] for t in self.transfers),
                               "unlocked_balance": self.unlocked,
                               "per_subaddress": [{"num_unspent_outputs": self.num_unspent}]}}
        if method == "incoming_transfers":
            return {"result": {"transfers": self.transfers}}
        if method == "get_address":
            return {"result": {"address": "4" + "A" * 94}}
        if method == "sweep_all":
            self.swept.append(params)
            return {"result": {"fee_list": [70700000], "tx_hash_list": ["ab" * 32]}}
        raise AssertionError(f"unexpected rpc {method}")


LIVE_STATE = [
    {"amount": 49000000000, "unlocked": False},      # 0.049 XMR — a deposit that just arrived
    {"amount": 330157500, "unlocked": False},        # change from the previous tip
    {"amount": 52182500, "unlocked": True},          # the only spendable output
]


def _run(wallet, monkeypatch):
    monkeypatch.setattr(keep, "rpc", wallet)
    return keep.main()


def test_the_live_wallet_gets_split(monkeypatch):
    """THE REPORTED FAILURE, end to end. Nominally three outputs, actually one — and the wallet had
    0.049 XMR unlocked by the time the timer next ran, so there was plenty to split."""
    wallet = FakeWallet(LIVE_STATE, unlocked_atomic=int(0.049 * XMR))
    assert _run(wallet, monkeypatch) == 0
    assert wallet.swept, (
        "the maintainer stood down on the exact state it exists to repair — one spendable output "
        "in a funded wallet, which is what the journal logged as 'outputs=3: nothing to do'")
    assert wallet.swept[0]["outputs"] >= 2


def test_it_still_waits_while_only_dust_is_unlocked(monkeypatch):
    """The state at the moment of measurement: the deposit had not unlocked yet. Sweeping then
    would pay a fee to split 0.0000522 XMR, and lock even that for ten blocks."""
    wallet = FakeWallet(LIVE_STATE, unlocked_atomic=52182500)
    assert _run(wallet, monkeypatch) == 0
    assert not wallet.swept, "swept a dust balance — that is a fee for nothing"


def test_a_healthy_wallet_is_not_swept(monkeypatch):
    """No fee churn: eight spendable outputs is the state this maintains, not one to act on."""
    healthy = [{"amount": 6 * 10 ** 9, "unlocked": True} for _ in range(8)]
    wallet = FakeWallet(healthy, unlocked_atomic=48 * 10 ** 9)
    assert _run(wallet, monkeypatch) == 0
    assert not wallet.swept


def test_a_wallet_reporting_many_unspent_but_none_spendable_is_split(monkeypatch):
    """The generalisation of the bug: any number of unspent outputs can be entirely locked. The old
    rule read a large `num_unspent_outputs` as health, and it says nothing about spendability."""
    all_locked = [{"amount": 10 ** 10, "unlocked": False} for _ in range(9)]
    all_locked.append({"amount": 5 * 10 ** 9, "unlocked": True})
    wallet = FakeWallet(all_locked, unlocked_atomic=5 * 10 ** 9)
    assert _run(wallet, monkeypatch) == 0
    assert wallet.swept, "ten unspent outputs, one spendable, and the maintainer did nothing"
