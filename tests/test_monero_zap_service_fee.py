"""THE OPERATOR'S CUT OF A CUSTODIAL ZAP.

Asked for as "I want a configurable fee for zaps sent to my monero address. should be in admin
settings, goes to my wallet", default 2%.

Three things decide whether a fee feature is safe, and all three are tested here:

1. **WHERE it can apply.** Only the custodial path — the one where this node executes the transfer.
   The address/QR flow is non-custodial: the payment goes from the sender's own wallet straight to
   the recipient and never touches this server, so there is nothing to take a cut of and no code
   here can pretend otherwise. The node's own wallet is not charged either: that is the operator
   paying themselves and losing a miner fee to do it.

2. **The ARITHMETIC.** A cut is money, so it is integer atomic units via Decimal, never floats,
   rounded DOWN so rounding always favours the recipient.

3. **That it FAILS OPEN.** No configured address, an address that will not validate, a nonsense
   percentage, or a cut that rounds below one atomic unit — every one of them results in the zap going
   out IN FULL. A fee that can strand somebody's tip is worse than no fee, and this is the property
   most likely to be quietly broken by a later edit.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.services.monero_user_wallets import (
    MAX_DESTINATIONS, UserWallets, split_fee, zap_fee_percent,
)

XMR = 10 ** 12
ADDR_A = "4" + "A" * 94
ADDR_B = "4" + "B" * 94
FEE_ADDR = "4" + "F" * 94


# ── the arithmetic ───────────────────────────────────────────────────────────────────────────────

def test_two_percent_of_a_hundredth_of_a_coin():
    net, cut = split_fee(XMR // 100, Decimal(2))
    assert (net, cut) == (9_800_000_000, 200_000_000)
    assert net + cut == XMR // 100, "the split must account for every atomic unit"


@pytest.mark.parametrize("atomic", [XMR, XMR // 3, 7 * XMR + 1, 123_456_789_012])
def test_nothing_is_ever_created_or_lost(atomic):
    net, cut = split_fee(atomic, Decimal("2.5"))
    assert net + cut == atomic


def test_rounding_favours_the_recipient():
    """A fraction of an atomic unit cannot be sent, so it must land on the person being tipped."""
    gross = 10 ** 9 + 7
    net, cut = split_fee(gross, Decimal("2"))
    assert cut == gross * 2 // 100      # rounded DOWN
    assert net == gross - cut


def test_a_cut_that_cannot_be_represented_is_not_taken():
    """A fraction of one atomic unit cannot be made into an output."""
    tiny = 49                 # 2% rounds down to zero atomic units
    net, cut = split_fee(tiny, Decimal(2))
    assert cut == 0 and net == tiny


def test_small_zaps_are_not_silently_exempt_from_the_service_fee():
    """The old 0.00001 XMR fee floor waived 2% on every zap below 0.0005 XMR."""
    gross = XMR // 10_000     # 0.0001 XMR -- a normal small zap
    net, cut = split_fee(gross, Decimal(2))
    assert cut == 2_000_000
    assert net + cut == gross


def test_a_fee_can_never_consume_the_whole_payment():
    net, cut = split_fee(1000, Decimal(100))
    assert cut == 0 and net == 1000


@pytest.mark.parametrize("atomic", [0, -1])
def test_a_nonpositive_amount_is_left_alone(atomic):
    assert split_fee(atomic, Decimal(2)) == (atomic, 0)


# ── the configured percentage ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ("2", Decimal(2)), ("2.5", Decimal("2.5")), ("0", Decimal(0)), ("", Decimal(0)),
    ("   ", Decimal(0)), ("nonsense", Decimal(0)), ("-5", Decimal(0)),
    ("200", Decimal(50)),          # far likelier a typo for 2.00 than an intention
    ("NaN", Decimal(0)),
])
def test_the_percentage_is_read_defensively(monkeypatch, raw, expect):
    monkeypatch.setenv("MONERO_ZAP_FEE_PERCENT", raw)
    assert zap_fee_percent() == expect


def test_the_default_is_two_percent(monkeypatch):
    monkeypatch.delenv("MONERO_ZAP_FEE_PERCENT", raising=False)
    assert zap_fee_percent() == Decimal(2)


# ── the whole pay path, against a fake wallet RPC ────────────────────────────────────────────────

class FakePool(UserWallets):
    def __init__(self, fee_to=FEE_ADDR, network="mainnet"):
        self.network = network
        self._fee_to = fee_to
        self.sent = []
        self._fee_address = None

    async def account(self, pubkey, create=True):
        return {"account_index": 3}

    async def fee_address(self):
        return self._fee_to

    async def rpc(self, method, params=None):
        assert method == "transfer_split"
        self.sent.append(params["destinations"])
        return {"tx_hash_list": ["ab" * 32],
                "amount_list": [sum(d["amount"] for d in params["destinations"])],
                "fee_list": [12345]}


def pay(pool, payments):
    """Drive the real `pay()`. No pytest-asyncio in this repo — `asyncio.run` is the convention
    here (tests/test_logs_scheduler.py does the same)."""
    return asyncio.run(pool.pay("npub1" + "q" * 58, payments))


def test_the_cut_is_taken_and_sent_to_the_operator(monkeypatch):
    monkeypatch.setenv("MONERO_ZAP_FEE_PERCENT", "2")
    pool = FakePool()
    out = pay(pool, [(ADDR_A, XMR // 100)])
    dests = pool.sent[0]
    assert {d["address"] for d in dests} == {ADDR_A, FEE_ADDR}
    by = {d["address"]: d["amount"] for d in dests}
    assert by[ADDR_A] == 9_800_000_000, "the recipient did not get the remainder"
    assert by[FEE_ADDR] == 200_000_000, "the operator did not get the cut"
    assert out["service_fee"] == 200_000_000 or out["service_fee"] == "0.0002"


def test_the_sender_is_debited_exactly_what_they_typed(monkeypatch):
    """The cut comes OUT of the amount. A surprise charge on top is the other way to do this and it
    debits somebody more than the number they entered."""
    monkeypatch.setenv("MONERO_ZAP_FEE_PERCENT", "2")
    pool = FakePool()
    pay(pool, [(ADDR_A, XMR // 100)])
    assert sum(d["amount"] for d in pool.sent[0]) == XMR // 100


def test_no_fee_address_means_the_zap_goes_out_in_full(monkeypatch):
    """FAIL OPEN. This is the property that matters most: the fee is the operator's arrangement and
    must never be able to stop a payment."""
    monkeypatch.setenv("MONERO_ZAP_FEE_PERCENT", "2")
    pool = FakePool(fee_to="")
    pay(pool, [(ADDR_A, XMR // 100)])
    assert pool.sent[0] == [{"address": ADDR_A, "amount": XMR // 100}]


def test_a_zero_percent_fee_changes_nothing(monkeypatch):
    monkeypatch.setenv("MONERO_ZAP_FEE_PERCENT", "0")
    pool = FakePool()
    pay(pool, [(ADDR_A, XMR // 100)])
    assert pool.sent[0] == [{"address": ADDR_A, "amount": XMR // 100}]


def test_paying_the_operator_is_not_charged(monkeypatch):
    """Otherwise the operator pays themselves a fee and burns a miner fee for the privilege."""
    monkeypatch.setenv("MONERO_ZAP_FEE_PERCENT", "2")
    pool = FakePool()
    pay(pool, [(FEE_ADDR, XMR // 100)])
    assert pool.sent[0] == [{"address": FEE_ADDR, "amount": XMR // 100}]


def test_a_zap_whose_fee_rounds_to_zero_is_sent_whole(monkeypatch):
    monkeypatch.setenv("MONERO_ZAP_FEE_PERCENT", "2")
    pool = FakePool()
    pay(pool, [(ADDR_A, 49)])
    assert pool.sent[0] == [{"address": ADDR_A, "amount": 49}]


def test_a_small_zap_reaches_the_operator(monkeypatch):
    monkeypatch.setenv("MONERO_ZAP_FEE_PERCENT", "2")
    pool = FakePool()
    gross = XMR // 10_000
    out = pay(pool, [(ADDR_A, gross)])
    by = {d["address"]: d["amount"] for d in pool.sent[0]}
    assert by == {ADDR_A: 98_000_000, FEE_ADDR: 2_000_000}
    assert out["service_fee"] == 2_000_000 or out["service_fee"] == "0.000002"


def test_one_aggregated_fee_output_per_transaction(monkeypatch):
    """Not one per recipient: a transaction's outputs are capped, so a cut per person would halve
    how many people a single zap can reach."""
    monkeypatch.setenv("MONERO_ZAP_FEE_PERCENT", "2")
    pool = FakePool()
    pay(pool, [(ADDR_A, XMR // 100), (ADDR_B, XMR // 100)])
    dests = pool.sent[0]
    assert len(dests) == 3
    assert sum(1 for d in dests if d["address"] == FEE_ADDR) == 1
    assert [d for d in dests if d["address"] == FEE_ADDR][0]["amount"] == 400_000_000


def test_a_big_batch_still_respects_the_destination_cap(monkeypatch):
    """The fee output takes a slot, so batches shrink by one — going over the cap is a refusal from
    the daemon, i.e. the whole zap fails."""
    monkeypatch.setenv("MONERO_ZAP_FEE_PERCENT", "2")
    pool = FakePool()
    pay(pool, [(ADDR_A, XMR // 100)] * 30)
    for chunk in pool.sent:
        assert len(chunk) <= MAX_DESTINATIONS, f"a transaction carried {len(chunk)} destinations"
    paid = sum(d["amount"] for chunk in pool.sent for d in chunk if d["address"] != FEE_ADDR)
    cut = sum(d["amount"] for chunk in pool.sent for d in chunk if d["address"] == FEE_ADDR)
    assert paid + cut == 30 * (XMR // 100), "atomic units went missing across the batches"


def test_every_recipient_in_a_big_batch_is_still_paid(monkeypatch):
    monkeypatch.setenv("MONERO_ZAP_FEE_PERCENT", "2")
    pool = FakePool()
    pay(pool, [(ADDR_A, XMR // 100)] * 30)
    paid = [d for chunk in pool.sent for d in chunk if d["address"] != FEE_ADDR]
    assert len(paid) == 30


# ── the address an operator saves in Admin ───────────────────────────────────────────────────────
#
# Reported as "i saved my address in admin, make sure persists and hydrates". Persistence is the
# settings store's job and it works; what did NOT work was this module, which cached the resolved
# address for the LIFE OF THE PROCESS on the reasoning that "an address does not change". It does —
# it is a field in Admin. Two silent failures came out of that: an address saved after the process
# had already resolved once had no effect until a restart (the fee just stayed off), and an address
# CHANGED later kept paying the old one. Reading a hydrated setting is a dict lookup.

class AddressPool(UserWallets):
    """Only the setting lookup and the network, which is all fee_address() needs."""

    def __init__(self, network="mainnet"):
        self.network = network
        self._fee_address = None
        self._fee_at = 0.0


def resolve(monkeypatch, value, network="mainnet"):
    monkeypatch.setenv("MONERO_ZAP_FEE_ADDRESS", value)
    return asyncio.run(AddressPool(network).fee_address())


def test_a_saved_address_is_used(monkeypatch):
    assert resolve(monkeypatch, FEE_ADDR) == FEE_ADDR


def test_changing_the_address_takes_effect_without_a_restart(monkeypatch):
    """THE BUG. The same pool instance must follow the setting, or an operator who corrects a typo
    carries on paying the address they just removed."""
    pool = AddressPool()
    monkeypatch.setenv("MONERO_ZAP_FEE_ADDRESS", FEE_ADDR)
    assert asyncio.run(pool.fee_address()) == FEE_ADDR
    other = "4" + "C" * 94
    monkeypatch.setenv("MONERO_ZAP_FEE_ADDRESS", other)
    assert asyncio.run(pool.fee_address()) == other, (
        "the pool is still using the previously resolved address — an operator changing it in "
        "Admin would keep sending their fee to the old one until the process restarted")


def test_clearing_the_address_stops_using_it(monkeypatch):
    """Blank means "use the node's own wallet", and with no node wallet reachable that means no fee.
    It must not keep paying the address that was just cleared."""
    pool = AddressPool()
    monkeypatch.setenv("MONERO_ZAP_FEE_ADDRESS", FEE_ADDR)
    assert asyncio.run(pool.fee_address()) == FEE_ADDR
    monkeypatch.setenv("MONERO_ZAP_FEE_ADDRESS", "")
    assert asyncio.run(pool.fee_address()) != FEE_ADDR


def test_an_address_for_the_wrong_network_is_refused(monkeypatch):
    """A mainnet address on a stagenet node (or the reverse) is not a small mistake — it is money
    sent somewhere unspendable. No fee is far better."""
    assert resolve(monkeypatch, FEE_ADDR, network="stagenet") == ""


def test_rubbish_in_the_field_takes_no_fee(monkeypatch):
    for junk in ("not an address", "4", "0" * 95, "   "):
        assert resolve(monkeypatch, junk) == ""


def test_whitespace_around_a_pasted_address_is_tolerated(monkeypatch):
    """Addresses get pasted, and a trailing newline should not silently disable the fee."""
    assert resolve(monkeypatch, "  " + FEE_ADDR + "\n") == FEE_ADDR


def test_the_setting_is_declared_so_it_hydrates_and_survives_a_save():
    """An undeclared key is dropped from the settings GET, loads blank on every visit, and is then
    written back empty by the next Save — the documented failure mode in CLAUDE.md."""
    from app.schemas import SettingsResponse
    fields = SettingsResponse.model_fields
    assert "monero_zap_fee_address" in fields
    assert "monero_zap_fee_percent" in fields


def test_the_admin_inputs_hydrate_and_save_the_same_key():
    """Hydration reads the element id, Save reads its name. They must agree or the field loads
    blank forever."""
    import re
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1]
            / "templates/admin/tabs/monero_wallet.html").read_text(encoding="utf-8")
    for key in ("monero_zap_fee_percent", "monero_zap_fee_address"):
        tag = re.search(r'<input[^>]*\b(?:id|name)="' + key + r'"[^>]*>', html)
        assert tag, f"{key} has no input in the Monero admin tab"
        assert f'id="{key}"' in tag.group(0) and f'name="{key}"' in tag.group(0), (
            f"{key}'s input must carry the same id and name")
