"""Moving money. Read the whole file before changing anything in it.

WHAT IS IMPLEMENTED AND WHAT IS NOT, SAID FIRST SO NOBODY DISCOVERS IT WITH A TRANSACTION. Sending
works on the EVM chains — Ethereum, Polygon, BNB Chain and Avalanche C-Chain — because one correct
implementation covers all four and the signing is done by `eth-account`, which is audited, widely
used and has published test vectors. The UTXO chains (BTC, LTC, DOGE, BCH) and Solana are REFUSED
with a sentence, not half-built: a Bitcoin spend is UTXO selection plus segwit sighashes, and a
wrong sighash is a signature over a transaction nobody meant to make. A refusal costs somebody a
disappointment; a bad spend costs them the coins.

FIVE THINGS THAT MUST BE RIGHT, and every one of them is read from the chain rather than assumed:

  * CHAIN ID — asked for with `eth_chainId` and never hardcoded from the symbol. A wrong one makes
    the transaction invalid, or worse, valid on a DIFFERENT chain, which is how a Polygon send
    becomes replayable on Ethereum.
  * NONCE — `pending`, so a second send while the first is unconfirmed does not collide. It is also
    the natural idempotency key: a retry at the same nonce REPLACES rather than duplicates, which is
    the property Monero's `/me/pay` lacks and why that path once charged twice.
  * GAS — estimated, then floored at 21000. An estimate that comes back low for a plain transfer is
    a provider being wrong, and an underpriced transaction is stuck rather than cheap.
  * THE BALANCE — checked against value PLUS the maximum fee, because a transaction that cannot pay
    its own worst case is rejected after it has been signed and broadcast.
  * THE RECIPIENT — checksum-validated. An address one character wrong is a valid-looking address
    nobody holds the key to, and the coins are simply gone.

AND THE RULE THAT OUTLIVES ALL OF THEM: A BROADCAST THAT TIMED OUT IS NOT A FAILED BROADCAST. This
codebase has already paid for that lesson once, on Monero: the client gave up at 20s over a live
transfer, reported "payment not sent", and invited a retry that would have been a second real
payment. So an uncertain send answers `unsure`, says the transaction may be on the chain, and names
the nonce to look for — never "it failed".
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.exodus_wallet_service import CHAINS, WalletError

logger = logging.getLogger(__name__)


class SendRefused(WalletError):
    """A send that will not be attempted, for a reason the person can act on."""


class SendUnsure(WalletError):
    """It may have gone through. Never presented as a failure — see the module docstring."""


#: A plain native-currency transfer. Contract calls are not something this wallet does.
TRANSFER_GAS = 21_000
#: Long enough for a busy RPC to answer, short enough not to hold a worker. Exceeding it on the
#: BROADCAST leg is `SendUnsure`, never a failure.
RPC_TIMEOUT = 20.0
BROADCAST_TIMEOUT = 45.0


def _client(timeout: float) -> httpx.AsyncClient:
    from app.services.proxy_utils import afallback_transport
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False,
                             transport=afallback_transport())


async def _rpc(client: httpx.AsyncClient, base: str, method: str, params: list) -> Any:
    r = await client.post(base, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    if r.status_code != 200:
        raise SendRefused(f"the {method} call was refused by this chain's node (HTTP {r.status_code})")
    d = r.json()
    if not isinstance(d, dict):
        raise SendRefused(f"this chain's node gave an unreadable answer to {method}")
    if d.get("error"):
        raise SendRefused(str((d["error"] or {}).get("message") or f"{method} was refused"))
    if "result" not in d:
        raise SendRefused(f"this chain's node gave no result for {method}")
    return d["result"]


def validate_recipient(symbol: str, address: str) -> str:
    """The address, normalised — or a refusal.

    Checksum-validated for EVM. An address one character wrong is still a well-formed address; it is
    simply one nobody holds the key to, and the coins sent there are gone with no way back. EIP-55
    is the only thing standing between a typo and that, so a mixed-case address that fails its
    checksum is refused rather than lowercased into acceptance.
    """
    spec = CHAINS.get(symbol)
    if not spec:
        raise SendRefused(f"unsupported chain {symbol!r}")
    if spec["kind"] != "evm":
        raise SendRefused(f"sending {symbol} is not supported yet")
    text = str(address or "").strip()
    try:
        from eth_utils import is_address, to_checksum_address
    except Exception as exc:  # noqa: BLE001
        raise SendRefused("this node has not installed the Ethereum library") from exc
    if not text.startswith("0x") or len(text) != 42 or not is_address(text):
        raise SendRefused("that is not an Ethereum-style address")
    if text != text.lower() and text != text.upper():
        # Mixed case means it carries a checksum. If it does not verify, it is a typo.
        try:
            if to_checksum_address(text) != text:
                raise SendRefused("that address's checksum does not match — check it character by character")
        except SendRefused:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SendRefused("that address could not be checked") from exc
    return to_checksum_address(text)


async def send_evm(*, symbol: str, private_key: bytes, to: str, units: int,
                   endpoint: str, from_address: str) -> dict[str, Any]:
    """Sign and broadcast one native-currency transfer. Returns the hash, or raises."""
    spec = CHAINS.get(symbol)
    if not spec or spec["kind"] != "evm":
        raise SendRefused(f"sending {symbol} is not supported yet")
    if units <= 0:
        raise SendRefused("amount must be greater than zero")
    if not endpoint:
        raise SendRefused(f"this node has no RPC endpoint configured for {symbol}")
    to = validate_recipient(symbol, to)

    try:
        from eth_account import Account
    except Exception as exc:  # noqa: BLE001
        raise SendRefused("this node has not installed the Ethereum library") from exc

    async with _client(RPC_TIMEOUT) as client:
        chain_id = int(str(await _rpc(client, endpoint, "eth_chainId", [])), 16)
        nonce = int(str(await _rpc(client, endpoint, "eth_getTransactionCount",
                                   [from_address, "pending"])), 16)
        balance = int(str(await _rpc(client, endpoint, "eth_getBalance", [from_address, "latest"])), 16)

        # Fees. EIP-1559 where the chain offers it, and a legacy gas price where it does not —
        # asked for rather than decided from the symbol, because an L2 or a fork can be either.
        try:
            tip = int(str(await _rpc(client, endpoint, "eth_maxPriorityFeePerGas", [])), 16)
        except WalletError:
            tip = None
        base_fee = None
        try:
            head = await _rpc(client, endpoint, "eth_getBlockByNumber", ["latest", False])
            if isinstance(head, dict) and head.get("baseFeePerGas"):
                base_fee = int(str(head["baseFeePerGas"]), 16)
        except WalletError:
            base_fee = None

        gas = TRANSFER_GAS
        try:
            est = int(str(await _rpc(client, endpoint, "eth_estimateGas",
                                     [{"from": from_address, "to": to, "value": hex(units)}])), 16)
            # A plain transfer cannot cost less than 21000. An estimate below it is a provider being
            # wrong, and honouring it produces a transaction the chain will not accept.
            gas = max(est, TRANSFER_GAS)
        except WalletError:
            gas = TRANSFER_GAS

        if base_fee is not None and tip is not None:
            # Room for the base fee to rise while the transaction is pending. Doubling is the
            # convention; the unused part is refunded, so the cost of being generous is nothing and
            # the cost of being tight is a stuck transaction.
            max_fee = base_fee * 2 + tip
            tx = {"to": to, "value": units, "gas": gas, "maxFeePerGas": max_fee,
                  "maxPriorityFeePerGas": tip, "nonce": nonce, "chainId": chain_id, "type": 2}
            worst = gas * max_fee
        else:
            price = int(str(await _rpc(client, endpoint, "eth_gasPrice", [])), 16)
            tx = {"to": to, "value": units, "gas": gas, "gasPrice": price,
                  "nonce": nonce, "chainId": chain_id}
            worst = gas * price

        # Checked BEFORE signing. A transaction that cannot pay its own worst case is rejected after
        # broadcast, which looks to the sender exactly like a bug in this app.
        if balance < units + worst:
            from app.services.exodus_wallet_service import from_base_units
            raise SendRefused(
                f"not enough {symbol} to cover the amount plus fees: holding "
                f"{from_base_units(balance, symbol)}, this needs "
                f"{from_base_units(units + worst, symbol)}")

        signed = Account.from_key(private_key).sign_transaction(tx)
        raw = signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction

    # BROADCAST ON ITS OWN CLIENT AND ITS OWN CLOCK. Everything above is a read and may be retried
    # freely; this one leg may not, and a timeout here is `unsure`, never a failure.
    try:
        async with _client(BROADCAST_TIMEOUT) as client:
            tx_hash = await _rpc(client, endpoint, "eth_sendRawTransaction", ["0x" + raw.hex()])
    except SendRefused:
        # The node said no, explicitly. Nothing was accepted, so this really is a failure.
        raise
    except Exception as exc:  # noqa: BLE001
        raise SendUnsure(
            f"the transaction was signed and sent, and this node did not hear back in time. It MAY "
            f"be on the chain — check for nonce {nonce} from your address before sending again, "
            f"because a second send at a different nonce would pay twice.") from exc

    return {"hash": str(tx_hash), "nonce": nonce, "chainId": chain_id,
            "gas": gas, "to": to, "units": units}
