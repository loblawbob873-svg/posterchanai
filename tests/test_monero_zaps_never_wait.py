"""A user with money can zap again WITHOUT waiting ~20 minutes — simulated against the chain's rules.

"User is still saying he had to wait 18min between monero zaps last night! … this monero problem can
never come back again! weeks of the same shit"

Every earlier fix was tested against a wallet SHAPE; the failure is a SEQUENCE over time. This drives
the SHIPPED `maintain_account_outputs` (ticking every minute, as the worker does) and `pay()` against
a wallet that follows Monero's rules: an output is spendable only 10 blocks after the block that mined
it, a block every 2 minutes, a transaction mines in the next block, a zap's change is ONE new output,
a split (sweep_single, outputs=N) is ONE transaction giving the account N outputs, and the wallet can
only spend UNLOCKED outputs. A zap that finds too little unlocked money is the 20-minute wait.

The starting state is the account MEASURED on 2026-09-22 (npub16jknk…, account 15): five 0.00001 XMR
dust outputs and one 0.2822 XMR output. The old maintainer counted six outputs, called it healthy and
never split; every zap then locked the only real output.
"""
import asyncio

import pytest

from app.services import monero_user_wallets as mod

XMR = 10**12
ACCOUNT = {"account_index": 15, "label": "pc:" + "a" * 64, "base_address": "4" + "A" * 94}
LOCK_BLOCKS, BLOCK_MIN, TX_FEE = 10, 2, 30_000_000


class Chain:
    def __init__(self, amounts):
        self.height, self.minute, self.rows, self.pending, self.n = 100, 0, [], [], 0
        for a in amounts:
            self.rows.append(self._row(a, "deposit", self.height - LOCK_BLOCKS))
        self.waits, self.splits, self.zaps = [], 0, 0

    def _row(self, amount, txh, height):
        self.n += 1
        return {"amount": int(amount), "spent": False, "frozen": False, "key_image": f"ki{self.n}",
                "tx_hash": txh, "height": height}

    def unlocked(self, r):
        return r["height"] is not None and self.height - r["height"] >= LOCK_BLOCKS

    def tick_minute(self):
        self.minute += 1
        if self.minute % BLOCK_MIN == 0:
            self.height += 1
            for txh, outs in self.pending:                      # mined in this block
                for a in outs:
                    self.rows.append(self._row(a, txh, self.height))
            self.pending = []

    async def rpc(self, method, params=None):
        params = params or {}
        if method == "get_accounts":
            return {"subaddress_accounts": [ACCOUNT]}
        assert params.get("account_index") == ACCOUNT["account_index"]
        if method == "incoming_transfers":
            want_spent = params["transfer_type"] == "unavailable"
            return {"transfers": [dict(r, unlocked=self.unlocked(r)) for r in self.rows
                                  if r["spent"] == want_spent]}
        if method == "get_transfers":
            return {"pending": [{"txid": t} for t, _ in self.pending]}
        if method == "sweep_single":
            src = next(r for r in self.rows if r["key_image"] == params["key_image"])
            assert not src["spent"] and self.unlocked(src), "split from a locked/spent output"
            src["spent"] = True
            n = int(params["outputs"]); each = (src["amount"] - TX_FEE) // n
            txh = f"split{self.n}"; self.pending.append((txh, [each] * n)); self.splits += 1
            return {"tx_hash": txh}
        if method == "transfer_split":
            need = sum(d["amount"] for d in params["destinations"]) + TX_FEE
            free = sorted((r for r in self.rows if not r["spent"] and self.unlocked(r)),
                          key=lambda r: r["amount"])
            pick = next(([r] for r in free if r["amount"] >= need), None)
            if pick is None:                                    # combine, largest first
                pick, tot = [], 0
                for r in sorted(free, key=lambda r: -r["amount"]):
                    pick.append(r); tot += r["amount"]
                    if tot >= need:
                        break
                if sum(r["amount"] for r in pick) < need:
                    self.waits.append(self.minute)
                    raise mod.WalletError("not enough unlocked money")
            for r in pick:
                r["spent"] = True
            change = sum(r["amount"] for r in pick) - need
            txh = f"zap{self.n}"; self.pending.append((txh, [change] if change > 0 else []))
            self.zaps += 1
            return {"tx_hash_list": [txh], "amount_list": [need - TX_FEE], "fee_list": [TX_FEE]}
        raise AssertionError(f"unexpected RPC {method}")


def run(chain, zap_minutes, warmup=30, amount=10**9):
    w = mod.UserWallets.__new__(mod.UserWallets)
    w.url, w.user, w.password, w.network = "rpc", "u", "p", "mainnet"
    w.timeout, w._fee_address, w._fee_at, w._lock = 1, None, 0, asyncio.Lock()
    w.rpc = chain.rpc

    async def go():
        end = warmup + max(zap_minutes) + 1
        for m in range(end):
            await w.maintain_account_outputs(ACCOUNT)           # the worker's 60 s tick
            if m - warmup in zap_minutes:
                try:
                    await w.pay("a" * 64, [("4" + "B" * 94, amount)])
                except mod.WalletError:
                    pass                                         # recorded in chain.waits
            chain.tick_minute()
    asyncio.run(go())
    return chain


@pytest.fixture(autouse=True)
def _no_fee_no_validation(monkeypatch):
    monkeypatch.setattr(mod, "validate_address", lambda *_: None)
    monkeypatch.setattr(mod, "zap_fee_percent", lambda: mod.Decimal(0))


MEASURED = [10**7] * 5 + [282_162_045_500]          # 5 x 0.00001 XMR dust + 0.28216 XMR


def test_last_nights_account_can_zap_every_few_minutes():
    """Six zaps three minutes apart from the measured account: none may wait."""
    c = run(Chain(MEASURED), [0, 3, 6, 9, 12, 15])
    assert c.waits == [], f"zaps had to wait (minutes {c.waits}); splits made: {c.splits}"
    assert c.zaps == 6


def test_last_nights_actual_schedule_never_waits():
    """The real times from 2026-09-22: 01:14, 01:38, 02:35, 03:10, 03:33 (+ a quick retry each)."""
    c = run(Chain(MEASURED), [0, 1, 24, 25, 81, 82, 116, 117, 139, 140])
    assert c.waits == [], f"zaps had to wait (minutes {c.waits})"


def test_a_zaps_own_change_never_blocks_the_top_up():
    """The second hole: ANY locked output was read as "a split is still unlocking", so the change of
    the zap just made stopped replenishment exactly while somebody was zapping. Two usable outputs
    left plus one locked single-output change must split now."""
    c = Chain([30 * XMR // 1000, 30 * XMR // 1000])
    c.rows.append(c._row(29 * XMR // 1000, "zap-change", c.height))      # locked, ONE output
    w = mod.UserWallets.__new__(mod.UserWallets)
    w.url, w.user, w.password, w.network = "rpc", "u", "p", "mainnet"
    w.timeout, w._fee_address, w._fee_at, w._lock = 1, None, 0, asyncio.Lock()
    w.rpc = c.rpc
    r = asyncio.run(w.maintain_account_outputs(ACCOUNT))
    assert r["action"] == "split", r


def test_a_split_that_is_still_unlocking_is_waited_for():
    c = Chain([30 * XMR // 1000])
    for _ in range(4):
        c.rows.append(c._row(5 * XMR // 1000, "split-tx", c.height))      # one tx, several outputs
    w = mod.UserWallets.__new__(mod.UserWallets)
    w.url, w.user, w.password, w.network = "rpc", "u", "p", "mainnet"
    w.timeout, w._fee_address, w._fee_at, w._lock = 1, None, 0, asyncio.Lock()
    w.rpc = c.rpc
    r = asyncio.run(w.maintain_account_outputs(ACCOUNT))
    assert r["action"] == "waiting" and c.splits == 0, r


def test_every_output_a_split_makes_can_pay_a_zap():
    c = Chain(MEASURED)
    run(c, [0], warmup=25)
    made = [r for r in c.rows if str(r["tx_hash"]).startswith("split")]
    assert made and all(r["amount"] >= mod.OUTPUT_USEFUL_ATOMIC for r in made), [r["amount"] for r in made]


def test_dust_never_counts_as_capacity():
    c = Chain(MEASURED)
    run(c, [0], warmup=1)
    assert c.splits >= 1, "an account whose only zap-sized output is one must be split"
