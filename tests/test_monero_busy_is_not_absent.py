"""A WALLET THAT IS BUSY IS NOT A WALLET THAT IS MISSING — the server half.

monero-wallet-rpc blocks while it scans, so on a node that is catching up every call times out.
`rpc()` mapped every httpx failure to one message, so "nothing is listening" and "it answered the
connection and is working hard" both became "Local Monero wallet is unavailable" — rendered as
"This device is in safe external-wallet mode", for hours, on a wallet that was fine.

The line between them is real and cheap: a CONNECT timeout or a refusal means nothing accepted the
socket. A READ timeout means something did, and is not answering yet.

It also decides whether the "still catching up" banner can appear at all. `sync_state` learns the
answer from `refresh`, and on a badly-behind wallet `refresh` is exactly the call that times out —
so treating that timeout as "unknown" would silence the banner precisely when it is true.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services import monero_wallet_service as svc


def wallet(raise_exc=None, reply=None):
    w = svc.MoneroWallet.__new__(svc.MoneroWallet)
    w.config = svc.WalletConfig(enabled=True, url="http://127.0.0.1:38083/json_rpc", username="u",
                                password="p", network="mainnet", transfer_cap_atomic=1,
                                daily_cap_atomic=1, timeout_seconds=2,
                                spend_ledger_path="data/x.sqlite3")

    async def rpc(method, params=None):
        if raise_exc is not None:
            raise raise_exc
        return reply or {}
    w.rpc = rpc
    return w


def test_busy_is_a_kind_of_wallet_error():
    """Callers that only know WalletError must keep working — this narrows the message, it does not
    introduce a failure mode nobody handles."""
    assert issubclass(svc.WalletBusy, svc.WalletError)


@pytest.mark.parametrize("exc,expect_busy", [
    (httpx.ReadTimeout("scanning"), True),
    (httpx.WriteTimeout("scanning"), True),
    (httpx.PoolTimeout("scanning"), True),
    (httpx.ConnectTimeout("no route"), False),
    (httpx.ConnectError("refused"), False),
])
def test_the_transport_failure_decides_which_one_it_is(exc, expect_busy, monkeypatch):
    """RUN the real `rpc` against a transport that fails the way httpx would."""
    cfg = svc.WalletConfig(enabled=True, url="http://127.0.0.1:38083/json_rpc", username="u",
                           password="p", network="mainnet", transfer_cap_atomic=1,
                           daily_cap_atomic=1, timeout_seconds=2,
                           spend_ledger_path="data/x.sqlite3")
    w = svc.MoneroWallet(cfg)

    class Boom(httpx.AsyncClient):
        async def post(self, *a, **k):
            raise exc

    monkeypatch.setattr(svc.httpx, "AsyncClient", Boom)
    with pytest.raises(svc.WalletError) as caught:
        asyncio.run(w.rpc("get_balance"))
    assert isinstance(caught.value, svc.WalletBusy) is expect_busy, (
        f"{type(exc).__name__} was classified wrongly — busy and absent must not read the same")


def test_a_refresh_that_times_out_is_reported_as_scanning():
    """The one that makes the banner possible at all. A wallet too busy to answer `refresh` is
    scanning; calling that 'unknown' hides the message in exactly the case it describes."""
    got = asyncio.run(wallet(raise_exc=svc.WalletBusy("busy")).sync_state())
    assert got["checked"] is True and got["scanning"] is True


def test_an_absent_wallet_is_still_unknown_rather_than_scanning():
    """A wallet that is not running is not 'catching up' — that would be a reassuring lie about a
    wallet nobody has started."""
    got = asyncio.run(wallet(raise_exc=svc.WalletError("unavailable")).sync_state())
    assert got["checked"] is False and got["scanning"] is None


def test_the_busy_message_is_the_one_the_client_recognises():
    """The client picks its card from this text, so the two have to agree. If this ever changes,
    the screen silently falls back to 'external-wallet mode' on a healthy wallet."""
    js = (__import__("pathlib").Path(__file__).resolve().parents[1]
          / "static/js/client/monero-wallet.js").read_text(encoding="utf-8")
    cfg = svc.WalletConfig(enabled=True, url="http://127.0.0.1:38083/json_rpc", username="u",
                           password="p", network="mainnet", transfer_cap_atomic=1,
                           daily_cap_atomic=1, timeout_seconds=2,
                           spend_ledger_path="data/x.sqlite3")
    w = svc.MoneroWallet(cfg)

    class Boom(httpx.AsyncClient):
        async def post(self, *a, **k):
            raise httpx.ReadTimeout("scanning")

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(svc.httpx, "AsyncClient", Boom)
        with _pytest.raises(svc.WalletBusy) as caught:
            asyncio.run(w.rpc("get_balance"))
    message = str(caught.value)
    assert "still reading the chain" in message
    assert "still reading the chain" in js, (
        "the client no longer recognises the server's busy message, so a scanning wallet is "
        "described as an absent one again")
