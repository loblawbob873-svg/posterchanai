"""What each chain says the wallet holds, and — more importantly — what it says when it cannot say.

THE ONE RULE THIS FILE EXISTS FOR: A PROVIDER THAT COULD NOT BE ASKED REPORTS `None`, NEVER 0.

A balance of zero and a balance nobody could fetch look identical on a screen and mean opposite
things. This codebase has written that lesson down repeatedly — the drive check ("could not ask" is
never "missing"), the uptime doc that refuses to persist an unread history, the contacts sweep whose
short list was a delete order. Here it is worse than a wrong number: somebody who sees 0.00 BTC
concludes their coins are gone, and somebody who sees 0.00 before sending concludes they can afford
nothing. So every reader below returns `None` on any failure, `Decimal`/int on a real answer, and the
caller renders the two differently. Nothing in this file ever turns an exception into a zero.

WHERE IT ASKS. Public block explorers and RPC endpoints, one per chain, every one overridable in
Admin so an operator can point at their own node — which is the same shape as every other outbound
integration here. Requests go through `afallback_transport` (the node's proxy → Tor, then direct),
so a balance lookup does not announce the operator's IP and every address they own to a third party
from the app server directly.

WHAT IT DOES NOT DO: build or sign transactions. That is a different file and a different level of
care; this one only reads.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx

from app.services.exodus_wallet_service import CHAINS, WalletError

logger = logging.getLogger(__name__)

#: Where each chain is read from when the operator has set nothing. Overridable per chain with the
#: `exodus_rpc_<symbol>` setting. These are public endpoints and they are a FALLBACK, not a plan: a
#: node that matters should point at its own.
DEFAULT_ENDPOINTS: dict[str, str] = {
    "BTC": "https://blockstream.info/api",
    "LTC": "https://litecoinspace.org/api",
    "DOGE": "https://api.blockcypher.com/v1/doge/main",
    "BCH": "https://free-bch.fullstack.cash",
    "ETH": "https://ethereum-rpc.publicnode.com",
    "MATIC": "https://polygon-bor-rpc.publicnode.com",
    "BNB": "https://bsc-rpc.publicnode.com",
    "AVAX": "https://avalanche-c-chain-rpc.publicnode.com",
    "SOL": "https://api.mainnet-beta.solana.com",
    "XRP": "https://xrplcluster.com",
}

#: Long enough for a slow explorer, short enough that nine chains in parallel cannot hold a request
#: open for a minute. A timeout is a "could not ask", not a zero.
TIMEOUT = 12.0


def endpoint_for(symbol: str, settings: dict[str, Any] | None = None) -> str:
    cfg = settings or {}
    override = str(cfg.get(f"exodus_rpc_{symbol.lower()}", "") or "").strip()
    return override or DEFAULT_ENDPOINTS.get(symbol, "")


def _client() -> httpx.AsyncClient:
    from app.services.proxy_utils import afallback_transport
    return httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False,
                             transport=afallback_transport())


async def _utxo_balance(client: httpx.AsyncClient, base: str, address: str, symbol: str) -> int | None:
    """Confirmed + unconfirmed, in the chain's smallest unit.

    Esplora's shape (Blockstream, litecoinspace and every mirror of it) reports funded and spent
    separately; the balance is the difference, and BOTH chains' numbers must be read or a wallet
    that has ever spent shows its lifetime received instead of what it has.
    """
    try:
        r = await client.get(f"{base.rstrip('/')}/address/{address}")
        if r.status_code != 200:
            return None
        d = r.json()
        # THE SHAPE MUST BE THE SHAPE, or a 200 that is not this API answers "you have nothing".
        # `.get(...) or {}` quietly turns a changed provider, an error document, or a captive
        # portal's HTML-parsed-as-JSON into funded=0/spent=0 — a confident zero for a wallet that
        # holds coins. Absent keys are an unreadable answer, which is what `None` means here.
        if not isinstance(d, dict) or not isinstance(d.get("chain_stats"), dict):
            return None
        chain = d["chain_stats"]
        mem = d.get('mempool_stats')
        if not isinstance(mem, dict):
            return None
        fields = [part.get(name) for part in (chain, mem) for name in ('funded_txo_sum', 'spent_txo_sum')]
        if any(type(value) is not int or value < 0 for value in fields):
            return None
        funded, spent = fields[0] + fields[2], fields[1] + fields[3]
        got = funded - spent
        # A negative balance is not a balance. It means the shape was misread, and reporting it as a
        # number would put a minus sign in front of somebody's money.
        return got if got >= 0 else None
    except Exception as exc:  # noqa: BLE001
        logger.info("[exodus] %s balance unavailable: %s", symbol, exc)
        return None


async def _evm_balance(client: httpx.AsyncClient, base: str, address: str, symbol: str) -> int | None:
    try:
        r = await client.post(base, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
                                          "params": [address, "latest"]})
        if r.status_code != 200:
            return None
        d = r.json()
        if not isinstance(d, dict) or "result" not in d or d.get("error"):
            return None
        value = d['result']
        if not isinstance(value, str) or not re.fullmatch(r'0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)', value):
            return None
        return int(value, 16)
    except Exception as exc:  # noqa: BLE001
        logger.info("[exodus] %s balance unavailable: %s", symbol, exc)
        return None


async def _sol_balance(client: httpx.AsyncClient, base: str, address: str, symbol: str) -> int | None:
    try:
        r = await client.post(base, json={"jsonrpc": "2.0", "id": 1, "method": "getBalance",
                                          "params": [address]})
        if r.status_code != 200:
            return None
        d = r.json()
        value = ((d or {}).get("result") or {}).get("value")
        return value if not d.get("error") and type(value) is int and value >= 0 else None
    except Exception as exc:  # noqa: BLE001
        logger.info("[exodus] %s balance unavailable: %s", symbol, exc)
        return None


async def _xrp_balance(client: httpx.AsyncClient, base: str, address: str, symbol: str) -> int | None:
    """Drops held, or None.

    TWO THINGS HERE THAT ARE NOT LIKE THE OTHER CHAINS:

    `actNotFound` IS A REAL ZERO, NOT A FAILURE. An XRP account does not exist until it has been
    funded past the reserve, and the ledger answers a lookup for one with that error. Treating it as
    "could not ask" would show `unavailable` to everybody who has generated a wallet and not yet
    received anything — which is exactly the person most likely to think the app is broken. Every
    OTHER error is still unknown.

    THE BALANCE IS NOT ALL SPENDABLE. XRPL holds a base reserve (1 XRP at the time of writing, and
    the ledger says so rather than this file) which can never be sent. Reporting the raw balance is
    honest about what is held; the send path is where the reserve has to be subtracted, and until
    XRP sending exists it is better to show what the ledger shows than to invent a second number.
    """
    try:
        r = await client.post(base, json={"method": "account_info",
                                          "params": [{"account": address, "ledger_index": "validated"}]})
        if r.status_code != 200:
            return None
        d = r.json()
        result = (d or {}).get("result") or {}
        if result.get("error") == "actNotFound":
            return 0
        if result.get("error"):
            return None
        data = result.get("account_data")
        if not isinstance(data, dict) or "Balance" not in data:
            return None
        got = int(str(data["Balance"]))
        return got if got >= 0 else None
    except Exception as exc:  # noqa: BLE001
        logger.info("[exodus] %s balance unavailable: %s", symbol, exc)
        return None



async def _doge_balance(client, base, address, symbol):
    try:
        response = await client.get(base.rstrip('/') + '/addrs/' + address + '/balance')
        response.raise_for_status()
        data = response.json()
        value = data.get('final_balance')
        return value if data.get('address') == address and type(value) is int and value >= 0 else None
    except Exception:
        return None


async def _bch_balance(client, base, address, symbol):
    try:
        response = await client.post(base.rstrip('/') + '/bch/balance', json={'addresses': [address]})
        response.raise_for_status()
        data = response.json()
        rows = data.get('balances')
        if data.get('success') is not True or not isinstance(rows, list) or len(rows) != 1:
            return None
        row = rows[0]
        if not isinstance(row, dict) or row.get('address') != address:
            return None
        balance = row.get('balance')
        if not isinstance(balance, dict):
            return None
        confirmed, unconfirmed = balance.get('confirmed'), balance.get('unconfirmed')
        if type(confirmed) is not int or type(unconfirmed) is not int or confirmed < 0:
            return None
        value = confirmed + unconfirmed
        return value if value >= 0 else None
    except Exception:
        return None


def _reader_for(symbol, settings):
    # Preserve existing custom Esplora integrations unless the operator explicitly chooses the
    # native public-provider protocol. Defaults now use each provider's actual response format.
    custom = str((settings or {}).get(f'exodus_rpc_{symbol.lower()}') or '').strip()
    api = str((settings or {}).get(f'exodus_api_{symbol.lower()}') or '').strip()
    if symbol in ('DOGE', 'BCH') and api != 'esplora' and (not custom or custom == DEFAULT_ENDPOINTS[symbol] or api in ('blockcypher', 'fullstack')):
        return _doge_balance if symbol == 'DOGE' else _bch_balance
    return _READERS.get(CHAINS[symbol]['kind'])

_READERS = {"utxo": _utxo_balance, "evm": _evm_balance, "sol": _sol_balance, "xrp": _xrp_balance}


async def balance(symbol: str, address: str, settings: dict[str, Any] | None = None) -> int | None:
    spec = CHAINS.get(symbol)
    if not spec:
        raise WalletError(f"unsupported chain {symbol!r}")
    base = endpoint_for(symbol, settings)
    if not base or not address:
        return None
    reader = _reader_for(symbol, settings)
    if not reader:
        return None
    async with _client() as client:
        return await reader(client, base, address, symbol)


async def balances(addresses: dict[str, str],
                   settings: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Every chain at once, and one slow explorer must not hold up the other eight.

    `gather` with `return_exceptions` so a reader that raises something unexpected is still an
    "unknown" for its own chain rather than an error for the whole screen — the failure mode this
    replaces is a holdings page that shows nothing because one endpoint was down.
    """
    from app.services.exodus_wallet_service import from_base_units

    symbols = [s for s in addresses if s in CHAINS]
    async with _client() as client:
        async def one(sym: str):
            spec, base = CHAINS[sym], endpoint_for(sym, settings)
            reader = _reader_for(sym, settings)
            if not base or not reader:
                return None
            return await reader(client, base, addresses[sym], sym)

        results = await asyncio.gather(*(one(s) for s in symbols), return_exceptions=True)

    out: dict[str, dict[str, Any]] = {}
    for sym, got in zip(symbols, results):
        units = None if isinstance(got, BaseException) else got
        if isinstance(got, BaseException):
            logger.info("[exodus] %s balance raised: %s", sym, got)
        out[sym] = {
            "address": addresses[sym],
            # `known` is the whole point. A client that reads `amount` without checking this will
            # print "0" for a chain nobody could reach.
            "known": units is not None,
            "units": units,
            "amount": from_base_units(units, sym) if units is not None else None,
        }
    return out
