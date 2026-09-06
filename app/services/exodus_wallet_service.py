"""A MULTI-CHAIN WALLET, MIRRORING WHAT THE EXODUS DESKTOP APP DOES: hold several coins, show what
you have, receive, and send.

WHOSE KEYS THESE ARE, PLAINLY: THIS NODE'S. One BIP-39 mnemonic per user, generated here, encrypted
at rest here, and used here to sign. That is what custodial means and no part of this design removes
it -- the same statement `monero_user_wallets.py` opens with, for the same reason. It is said in the
UI too, not only in a docstring.

WHY NOT IN THE BROWSER, which would have been self-custody and is what Exodus itself does. The
client is a bundle served under a CSP that forbids third-party script origins, so every byte of
crypto would have to be vendored -- and it is not one library: BIP-39, BIP-32, secp256k1 point
arithmetic, ripemd160, keccak256, bech32 and base58check, and then the part that actually moves
money, which is UTXO selection with segwit sighashes for Bitcoin and RLP with EIP-1559 for Ethereum.
The vendored nostr bundle contains noble's curves and hashes but exports only nostr-shaped helpers,
so none of it is reachable. Hand-writing that stack is the way people lose money, and a wallet whose
correctness nobody can vouch for is worse than one whose custody is stated. `bip_utils` is a
maintained implementation with published test vectors; this module is a thin, auditable layer over
it.

WHAT THE SEED IS ENCRYPTED WITH, AND WHY IT IS NOT IN A RELAY DOC. Every other private thing here is
a replaceable kind-30078 document, and this file deliberately breaks that pattern once. A
replaceable doc is REPLACED by whatever is written next, and this codebase has recorded the same
accident more than once: an unreachable relay answers an empty read, the empty read is written back,
and the document is gone. For a mute list that costs a re-follow. For a wallet seed it costs every
coin behind it, permanently, with no way to reconstruct it from anything. So the ciphertext lives in
a row, which is transactional and backed up, and the KEY that opens it is the per-user storage key
from `keystore` -- a file, not a database column -- so a stolen database dump alone decrypts
nothing.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

#: What an amount may look like: digits, optionally a single decimal point, nothing else.
#: No sign, no exponent, no separators — see `to_base_units`.
_PLAIN_DECIMAL = re.compile(r"\d+(?:\.\d+)?|\.\d+")


class WalletError(Exception):
    """Something the caller can be told about without leaking a secret."""


class WalletLocked(WalletError):
    """There is no wallet for this account yet, or its key cannot be read."""


#: What this wallet knows how to hold. The name is the client-facing symbol; `coin` is the bip_utils
#: enum member resolved lazily, because importing bip_utils costs ~0.4s and most requests here never
#: touch a key. `decimals` is what one whole unit is worth in the chain's smallest integer, and it
#: belongs BESIDE the coin rather than in the client: a display that disagrees with the chain about
#: where the decimal point goes is a wrong balance, and every one of these is a different number.
CHAINS: dict[str, dict[str, Any]] = {
    "BTC":  {"name": "Bitcoin",      "coin": "BITCOIN",              "decimals": 8,  "kind": "utxo"},
    "ETH":  {"name": "Ethereum",     "coin": "ETHEREUM",             "decimals": 18, "kind": "evm"},
    "LTC":  {"name": "Litecoin",     "coin": "LITECOIN",             "decimals": 8,  "kind": "utxo"},
    "DOGE": {"name": "Dogecoin",     "coin": "DOGECOIN",             "decimals": 8,  "kind": "utxo"},
    "BCH":  {"name": "Bitcoin Cash", "coin": "BITCOIN_CASH",         "decimals": 8,  "kind": "utxo"},
    "MATIC": {"name": "Polygon",     "coin": "POLYGON",              "decimals": 18, "kind": "evm"},
    "BNB":  {"name": "BNB Chain",    "coin": "BINANCE_SMART_CHAIN",  "decimals": 18, "kind": "evm"},
    "AVAX": {"name": "Avalanche",    "coin": "AVAX_C_CHAIN",         "decimals": 18, "kind": "evm"},
    "SOL":  {"name": "Solana",       "coin": "SOLANA",               "decimals": 9,  "kind": "sol"},
    # XRP's smallest unit is a "drop", 1e-6 XRP — six decimals, not eight and not eighteen. Getting
    # that wrong by two places is a hundredfold error in an amount somebody is sending.
    "XRP":  {"name": "XRP",          "coin": "RIPPLE",               "decimals": 6,  "kind": "xrp"},
}

#: Monero is here, and it is NOT one of the derived chains above. This node already runs a real
#: per-user Monero wallet — its own daemon, its own outputs, its own caps and spend ledger
#: (`monero_user_wallets.py`). Deriving a second XMR key from this seed would give one person TWO
#: unrelated balances and TWO addresses, and the one they were shown last is the one they would
#: publish; money sent to the other would look like a loss. So the wallet screen shows the wallet
#: that already exists, reading its real address and balance, rather than inventing a second.
MONERO = {"symbol": "XMR", "name": "Monero", "decimals": 12, "kind": "node-wallet"}

#: Nothing is excluded any more: Monero is shown, backed by the node's existing wallet rather than by
#: a key derived here. Kept as an empty map because `/status` publishes it and a client that has not
#: been redeployed still reads the field.
EXCLUDED: dict[str, str] = {}


def supported() -> list[dict[str, Any]]:
    """The catalogue, safe to hand to a client. Contains no key material and no user data.

    Monero is appended rather than living in CHAINS, because everything in CHAINS is derived from
    the seed and Monero is not — see the note on MONERO. Its `kind` says so, so the screen can label
    it and the send path can route it to the wallet that owns it.
    """
    rows = [{"symbol": s, "name": c["name"], "decimals": c["decimals"], "kind": c["kind"]}
            for s, c in CHAINS.items()]
    rows.append(dict(MONERO))
    return rows


# ---------------------------------------------------------------- the seed, and what guards it
def _aead(storage_seckey: bytes) -> AESGCM:
    """A key for THIS purpose, derived from the account's storage key.

    HKDF with a purpose string rather than the storage key itself: that key already encrypts chats
    and uploads, and a single key doing several jobs means a flaw in any one of them is a flaw in
    all. The salt is fixed and the info string is not — that is the way round HKDF wants, and it is
    what makes these outputs unrelated to the ones the chat path derives.
    """
    if not storage_seckey or len(storage_seckey) < 32:
        raise WalletLocked("this account has no storage key")
    key = HKDF(algorithm=SHA256(), length=32, salt=b"pcai:exodus:v1",
               info=b"exodus-wallet-seed").derive(storage_seckey)
    return AESGCM(key)


def seal(mnemonic: str, storage_seckey: bytes) -> bytes:
    """Encrypt a mnemonic for storage. The nonce is random and travels with the ciphertext."""
    if not mnemonic or not mnemonic.strip():
        raise WalletError("refusing to store an empty mnemonic")
    nonce = os.urandom(12)
    return nonce + _aead(storage_seckey).encrypt(nonce, mnemonic.strip().encode("utf-8"), None)


def unseal(blob: bytes, storage_seckey: bytes) -> str:
    """Decrypt a stored mnemonic.

    A failure here is NEVER 'there is no wallet'. The two are the difference between showing
    somebody a create-a-wallet button and telling them their coins are unreachable, and a caller
    that cannot tell them apart will offer to generate a second seed over the top of the first.
    """
    if not blob or len(blob) < 13:
        raise WalletLocked("the stored wallet is unreadable")
    try:
        return _aead(storage_seckey).decrypt(blob[:12], blob[12:], None).decode("utf-8")
    except Exception as exc:                                  # noqa: BLE001 - opaque on purpose
        raise WalletLocked("the stored wallet could not be decrypted") from exc


def new_mnemonic(words: int = 12) -> str:
    """A fresh BIP-39 phrase from the library's own generator.

    Twelve words by default because that is what Exodus writes down and what every wallet this seed
    might later be restored into accepts. The entropy comes from bip_utils, which takes it from the
    OS — deliberately not from anything in this file.
    """
    from bip_utils import Bip39MnemonicGenerator, Bip39WordsNum
    if words not in (12, 15, 18, 21, 24):
        raise WalletError("a BIP-39 phrase is 12, 15, 18, 21 or 24 words")
    num = getattr(Bip39WordsNum, f"WORDS_NUM_{words}")
    return str(Bip39MnemonicGenerator().FromWordsNumber(num))


def validate_mnemonic(mnemonic: str) -> bool:
    """Is this a real BIP-39 phrase, checksum and all?

    Checked BEFORE anything is stored, because a phrase that is one wrong word derives a perfectly
    valid — and completely different — set of addresses. Somebody restoring a wallet would be shown
    an empty balance for an account that is not theirs, with nothing to say why.
    """
    from bip_utils import Bip39MnemonicValidator
    try:
        return bool(Bip39MnemonicValidator().IsValid(str(mnemonic or "").strip()))
    except Exception:                                         # noqa: BLE001
        return False


# ---------------------------------------------------------------- deriving what the chains need
def _seed_bytes(mnemonic: str) -> bytes:
    from bip_utils import Bip39SeedGenerator
    return Bip39SeedGenerator(mnemonic).Generate()


def _account(seed: bytes, symbol: str, index: int = 0):
    from bip_utils import Bip44, Bip44Changes, Bip44Coins
    spec = CHAINS.get(symbol)
    if not spec:
        raise WalletError(f"unsupported chain {symbol!r}")
    coin = getattr(Bip44Coins, spec["coin"], None)
    if coin is None:
        raise WalletError(f"this build of bip_utils does not know {symbol}")
    return (Bip44.FromSeed(seed, coin).Purpose().Coin()
            .Account(0).Change(Bip44Changes.CHAIN_EXT).AddressIndex(int(index)))


def address_for(mnemonic: str, symbol: str, index: int = 0) -> str:
    """The receive address for one chain."""
    return _account(_seed_bytes(mnemonic), symbol, index).PublicKey().ToAddress()


def addresses(mnemonic: str, index: int = 0) -> dict[str, str]:
    """Every supported chain's receive address, derived once from one seed.

    The seed is expanded ONCE and reused across chains: `Bip39SeedGenerator` is PBKDF2 with 2048
    iterations, and paying that per coin turned a nine-chain listing into nine times the work for an
    identical result.
    """
    seed = _seed_bytes(mnemonic)
    out: dict[str, str] = {}
    for symbol in CHAINS:
        try:
            out[symbol] = _account(seed, symbol, index).PublicKey().ToAddress()
        except Exception as exc:                              # noqa: BLE001
            # One chain the installed bip_utils cannot do must not cost the other eight.
            logger.warning("[exodus] %s address derivation failed: %s", symbol, exc)
    return out


def private_key_for(mnemonic: str, symbol: str, index: int = 0) -> bytes:
    """The signing key for one address. Callers must not log, return or persist this."""
    return _account(_seed_bytes(mnemonic), symbol, index).PrivateKey().Raw().ToBytes()


# ---------------------------------------------------------------- amounts
def to_base_units(amount: str, symbol: str) -> int:
    """A decimal string to the chain's smallest integer, EXACTLY.

    Decimal, never float: 0.1 + 0.2 is not 0.3 in binary floating point, and a wallet that rounds
    a send is a wallet that sends the wrong number. Rejects more precision than the chain has rather
    than silently truncating it — somebody who typed nine decimals of BTC meant something, and
    quietly dropping the ninth is a different amount than they asked for.
    """
    from decimal import Decimal, InvalidOperation
    spec = CHAINS.get(symbol)
    if not spec:
        raise WalletError(f"unsupported chain {symbol!r}")
    text = str(amount).strip()
    # PLAIN DECIMAL ONLY. `Decimal` is happy to read "1e9", and so a slipped keystroke in an amount
    # box becomes a billion units — a number nobody typed and the chain will honour. Exponent
    # notation is not something a person enters when they mean an amount of money, so it is refused
    # rather than interpreted.
    if not _PLAIN_DECIMAL.fullmatch(text):
        raise WalletError("that is not an amount")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise WalletError("that is not an amount") from exc
    if value <= 0:
        raise WalletError("amount must be greater than zero")
    scaled = value * (Decimal(10) ** spec["decimals"])
    if scaled != scaled.to_integral_value():
        raise WalletError(f"{symbol} has {spec['decimals']} decimal places")
    return int(scaled)


def from_base_units(units: int, symbol: str) -> str:
    """The chain's integer back to a decimal string, with no exponent and no trailing noise."""
    from decimal import Decimal
    spec = CHAINS.get(symbol)
    if not spec:
        raise WalletError(f"unsupported chain {symbol!r}")
    value = Decimal(int(units)) / (Decimal(10) ** spec["decimals"])
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
