"""Versioned recovery keys and exact coin amounts for the CloudOS wallet.

Recovery phrases are encrypted with the account's server-held storage key. The server
signs transactions, and users can export backups. Existing CloudOS address formats
remain readable; exodus_derivation implements the independently tested Exodus paths.
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

# Monero uses its own spend/view keys derived from THIS wallet's BIP-39 seed.
# It never shares keys, accounts, RPC sessions, or balances with the built-in tipping wallet.
MONERO = {"symbol": "XMR", "name": "Monero", "decimals": 12, "kind": "monero"}
EXCLUDED: dict[str, str] = {}


def supported() -> list[dict[str, Any]]:
    """Public asset catalogue, without internal derivation parameters or keys."""
    return [{"symbol": s, "name": c["name"], "decimals": c["decimals"], "kind": c["kind"]}
            for s, c in CHAINS.items()] + [dict(MONERO)]


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


def _account(seed: bytes, symbol: str, index: int = 0, account: int = 0):
    if type(account) is not int or not 0 <= account < 16:
        raise WalletError("invalid portfolio index")
    from bip_utils import Bip44, Bip44Changes, Bip44Coins
    spec = CHAINS.get(symbol)
    if not spec:
        raise WalletError(f"unsupported chain {symbol!r}")
    coin = getattr(Bip44Coins, spec["coin"], None)
    if coin is None:
        raise WalletError(f"this build of bip_utils does not know {symbol}")
    return (Bip44.FromSeed(seed, coin).Purpose().Coin()
            .Account(account).Change(Bip44Changes.CHAIN_EXT).AddressIndex(int(index)))


def monero_keys(mnemonic: str, account: int = 0, recovery: str | None = None):
    """Version-one XMR derivation: secp256k1 BIP-44 m/44'/128'/account'/0/0,
    followed by Monero's Keccak/scalar conversion. No built-in-wallet state is consulted.
    """
    if type(account) is not int or not 0 <= account < 16:
        raise WalletError("invalid portfolio index")
    from bip_utils import Bip44, Bip44Coins, Bip44Changes, Monero, MoneroMnemonicDecoder
    if recovery:
        try:
            return Monero.FromPrivateSpendKey(MoneroMnemonicDecoder().Decode(recovery))
        except Exception as error:
            raise WalletError("Invalid Monero recovery words; check the words and checksum") from error
    key = (Bip44.FromSeed(_seed_bytes(mnemonic), Bip44Coins.MONERO_SECP256K1)
           .Purpose().Coin().Account(account).Change(Bip44Changes.CHAIN_EXT).AddressIndex(0))
    return Monero.FromBip44PrivateKey(key.PrivateKey().Raw().ToBytes())


def address_for(mnemonic: str, symbol: str, index: int = 0, account: int = 0) -> str:
    """The receive address for one chain."""
    return _account(_seed_bytes(mnemonic), symbol, index, account).PublicKey().ToAddress()


def addresses(mnemonic: str, index: int = 0, account: int = 0) -> dict[str, str]:
    """Every supported chain's receive address, derived once from one seed.

    The seed is expanded ONCE and reused across chains: `Bip39SeedGenerator` is PBKDF2 with 2048
    iterations, and paying that per coin turned a nine-chain listing into nine times the work for an
    identical result.
    """
    seed = _seed_bytes(mnemonic)
    out: dict[str, str] = {}
    for symbol in CHAINS:
        try:
            out[symbol] = _account(seed, symbol, index, account).PublicKey().ToAddress()
        except Exception as exc:                              # noqa: BLE001
            # One chain the installed bip_utils cannot do must not cost the other eight.
            logger.warning("[exodus] %s address derivation failed: %s", symbol, exc)
    return out


def private_key_for(mnemonic: str, symbol: str, index: int = 0, account: int = 0) -> bytes:
    """The signing key for one address. Callers must not log, return or persist this."""
    return _account(_seed_bytes(mnemonic), symbol, index, account).PrivateKey().Raw().ToBytes()


# ---------------------------------------------------------------- amounts
def to_base_units(amount: str, symbol: str) -> int:
    """Parse decimal text without the process-wide Decimal precision or binary floats."""
    spec = MONERO if symbol == 'XMR' else CHAINS.get(symbol)
    if not spec:
        raise WalletError(f"unsupported chain {symbol!r}")
    text = str(amount).strip()
    if len(text) > 256 or not _PLAIN_DECIMAL.fullmatch(text):
        raise WalletError("that is not an amount")
    whole, _, fractional = text.partition('.')
    fractional = fractional.rstrip('0')
    decimals = spec['decimals']
    if len(fractional) > decimals:
        raise WalletError(f"{symbol} has {decimals} decimal places")
    units = int(whole) * 10**decimals + int(fractional.ljust(decimals, '0') or '0')
    if units <= 0:
        raise WalletError("amount must be greater than zero")
    return units


def from_base_units(units: int, symbol: str) -> str:
    """Format integer units exactly, even beyond Decimal's default 28-digit precision."""
    spec = MONERO if symbol == 'XMR' else CHAINS.get(symbol)
    if not spec:
        raise WalletError(f"unsupported chain {symbol!r}")
    if type(units) is not int or units < 0:
        raise WalletError('invalid balance')
    decimals = spec['decimals']
    whole, fraction = divmod(units, 10**decimals)
    tail = str(fraction).zfill(decimals).rstrip('0')
    return str(whole) + ('.' + tail if tail else '')
