"""A BALANCE NOBODY COULD FETCH MUST NOT LOOK LIKE ZERO.

That is the whole reason this module has tests. Zero and unknown render identically and mean
opposite things: somebody who sees 0.00 BTC concludes their coins are gone, and somebody who sees
0.00 before sending concludes they can afford nothing. This codebase has written the lesson down
repeatedly elsewhere -- the drive check's "could not ask is never missing", the uptime doc that
refuses to persist an unread history, the contacts sweep whose short list was a delete order -- and
here the number is money.

Every reader is driven against a stubbed transport, because the failures that matter are the ones a
live endpoint will not produce on demand: a 500, a timeout, malformed JSON, an RPC error object, a
negative balance from a misread shape.
"""
import asyncio

import httpx
import pytest

from app.services import exodus_chain_service as C


def _stub(handler):
    """Run the service against a transport that answers however the test wants."""
    return httpx.MockTransport(handler)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing here may touch a real endpoint."""
    monkeypatch.setattr(C, "_client", lambda: httpx.AsyncClient(
        transport=_stub(lambda r: httpx.Response(599, text="no test set a handler"))))


def _with(handler):
    C._client = lambda: httpx.AsyncClient(transport=_stub(handler))


# ── the rule ──────────────────────────────────────────────────────────────────────────────────
# `{"unexpected":true}` and `[]` are the ones that matter: a 200 whose SHAPE is not this API used to
# come back as a confident zero, which is what a changed provider or a captive portal looks like.
@pytest.mark.parametrize("status,body", [(500, "{}"), (404, "{}"), (200, "not json"),
                                         (200, '{"unexpected":true}'), (200, "[]"),
                                         (200, '{"chain_stats":{}}'), (429, "slow down")])
def test_an_unreachable_provider_is_unknown_not_zero(status, body):
    _with(lambda r: httpx.Response(status, text=body))
    assert _run(C.balance("BTC", "1abc")) is None


def test_a_timeout_is_unknown_not_zero():
    def boom(_r): raise httpx.ReadTimeout("too slow")
    _with(boom)
    assert _run(C.balance("BTC", "1abc")) is None
    assert _run(C.balance("ETH", "0xabc")) is None
    assert _run(C.balance("SOL", "abc")) is None


def test_an_rpc_error_object_is_unknown_not_zero():
    """A JSON-RPC 200 carrying an `error` is a refusal, and `result` is absent."""
    _with(lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                              "error": {"code": -32000, "message": "nope"}}))
    assert _run(C.balance("ETH", "0xabc")) is None


def test_a_real_zero_is_reported_as_zero():
    """The other half: a chain that genuinely holds nothing must say so, not 'unknown'."""
    _with(lambda r: httpx.Response(200, json={"chain_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0},
                                              "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0}}))
    assert _run(C.balance("BTC", "1abc")) == 0


# ── reading each shape correctly ──────────────────────────────────────────────────────────────
def test_a_utxo_balance_subtracts_what_was_spent():
    """Funded alone is LIFETIME RECEIVED. A wallet that has ever spent would show far too much."""
    _with(lambda r: httpx.Response(200, json={
        "chain_stats": {"funded_txo_sum": 500_000, "spent_txo_sum": 200_000},
        "mempool_stats": {"funded_txo_sum": 10_000, "spent_txo_sum": 1_000}}))
    assert _run(C.balance("BTC", "1abc")) == 309_000


def test_a_negative_utxo_balance_is_refused():
    """Not a balance -- it means the shape was misread, and reporting it puts a minus sign in front
    of somebody's money."""
    _with(lambda r: httpx.Response(200, json={
        "chain_stats": {"funded_txo_sum": 1, "spent_txo_sum": 5},
        "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0}}))
    assert _run(C.balance("BTC", "1abc")) is None


def test_an_evm_balance_is_read_as_hex_wei():
    _with(lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0xde0b6b3a7640000"}))
    assert _run(C.balance("ETH", "0xabc")) == 10 ** 18


def test_a_solana_balance_is_lamports():
    _with(lambda r: httpx.Response(200, json={"result": {"value": 1_500_000_000}}))
    assert _run(C.balance("SOL", "abc")) == 1_500_000_000


# ── the whole screen ──────────────────────────────────────────────────────────────────────────
def test_one_dead_chain_does_not_cost_the_others():
    """The failure this replaces is a holdings page showing nothing because one endpoint was down."""
    def mixed(request):
        if "blockstream" in str(request.url):
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})
    _with(mixed)
    got = _run(C.balances({"BTC": "1abc", "ETH": "0xabc"}))
    assert got["BTC"]["known"] is False and got["BTC"]["amount"] is None
    assert got["ETH"]["known"] is True and got["ETH"]["units"] == 1


def test_every_row_says_whether_it_is_known():
    """A client that reads `amount` without checking `known` prints 0 for an unreachable chain."""
    _with(lambda r: httpx.Response(500))
    for row in _run(C.balances({"BTC": "1abc", "ETH": "0xabc", "SOL": "abc"})).values():
        assert set(row) >= {"address", "known", "units", "amount"}
        assert row["known"] is False and row["units"] is None and row["amount"] is None


def test_an_operator_endpoint_overrides_the_public_default():
    assert C.endpoint_for("BTC", {"exodus_rpc_btc": "https://my.node/api"}) == "https://my.node/api"
    assert C.endpoint_for("BTC", {}) == C.DEFAULT_ENDPOINTS["BTC"]
    assert C.endpoint_for("BTC", {"exodus_rpc_btc": "   "}) == C.DEFAULT_ENDPOINTS["BTC"]


def test_a_chain_with_no_endpoint_is_unknown_rather_than_an_error():
    assert _run(C.balance("BTC", "1abc", {"exodus_rpc_btc": ""})) is not False
    assert C.endpoint_for("NOPE", {}) == ""


def test_an_unsupported_chain_is_refused():
    from app.services.exodus_wallet_service import WalletError
    with pytest.raises(WalletError):
        _run(C.balance("NOPE", "x"))
