"""Native EVM transfers with network validation and conservative broadcast outcomes.

Reads may fail before signing. Once broadcast starts, only the matching locally computed
transaction hash confirms acceptance; every other outcome is uncertain and must not invite
another payment. The pending nonce prevents replacement collisions, but is not a substitute
for a durable request ledger across retries or server workers.
"""
from __future__ import annotations

import logging
import re
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
NETWORKS = {"ETH": 1, "MATIC": 137, "BNB": 56, "AVAX": 43114}


def _quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value):
        raise SendRefused("the network returned an invalid quantity")
    result = int(value, 16)
    if result >= 2**256:
        raise SendRefused("the network returned an oversized quantity")
    return result
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
    if type(units) is not int or not 0 < units < 2**256:
        raise SendRefused("amount must be greater than zero")
    if not endpoint:
        raise SendRefused(f"this node has no RPC endpoint configured for {symbol}")
    to = validate_recipient(symbol, to)

    try:
        from eth_account import Account
    except Exception as exc:  # noqa: BLE001
        raise SendRefused("this node has not installed the Ethereum library") from exc

    if Account.from_key(private_key).address.lower() != str(from_address).lower():
        raise SendRefused("the sender address does not match the selected wallet")

    async with _client(RPC_TIMEOUT) as client:
        chain_id = _quantity(await _rpc(client, endpoint, "eth_chainId", []))
        if chain_id != NETWORKS.get(symbol):
            raise SendRefused(f"the RPC network does not match {symbol}")
        nonce = _quantity(await _rpc(client, endpoint, "eth_getTransactionCount",
                                   [from_address, "pending"]))
        balance = _quantity(await _rpc(client, endpoint, "eth_getBalance", [from_address, "pending"]))

        # Fees. EIP-1559 where the chain offers it, and a legacy gas price where it does not —
        # asked for rather than decided from the symbol, because an L2 or a fork can be either.
        try:
            tip = _quantity(await _rpc(client, endpoint, "eth_maxPriorityFeePerGas", []))
        except WalletError:
            tip = None
        base_fee = None
        try:
            head = await _rpc(client, endpoint, "eth_getBlockByNumber", ["latest", False])
            if isinstance(head, dict) and head.get("baseFeePerGas"):
                base_fee = _quantity(head["baseFeePerGas"])
        except WalletError:
            base_fee = None

        gas = max(TRANSFER_GAS, _quantity(await _rpc(client, endpoint, "eth_estimateGas",
                  [{"from": from_address, "to": to, "value": hex(units)}])))

        if base_fee is not None and tip is not None:
            # Room for the base fee to rise while the transaction is pending. Doubling is the
            # convention; the unused part is refunded, so the cost of being generous is nothing and
            # the cost of being tight is a stuck transaction.
            max_fee = base_fee * 2 + tip
            tx = {"to": to, "value": units, "gas": gas, "maxFeePerGas": max_fee,
                  "maxPriorityFeePerGas": tip, "nonce": nonce, "chainId": chain_id, "type": 2}
            worst = gas * max_fee
        else:
            price = _quantity(await _rpc(client, endpoint, "eth_gasPrice", []))
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

    from eth_utils import keccak
    local_hash = "0x" + keccak(raw).hex()
    try:
        async with _client(BROADCAST_TIMEOUT) as client:
            tx_hash = await _rpc(client, endpoint, "eth_sendRawTransaction", ["0x" + raw.hex()])
        if not isinstance(tx_hash, str) or tx_hash.lower() != local_hash:
            raise ValueError("unconfirmed transaction hash")
    except Exception as exc:
        # HTTP errors, malformed replies and 'already known' can all follow acceptance.
        raise SendUnsure(
            f"Transaction {local_hash} may be on the network. Check this hash and nonce {nonce} "
            f"before sending again; another payment could pay twice.") from exc

    return {"hash": local_hash, "nonce": nonce, "chainId": chain_id,
            "gas": gas, "to": to, "units": units}
