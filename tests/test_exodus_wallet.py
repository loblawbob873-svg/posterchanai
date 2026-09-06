"""The multi-chain wallet's core: seeds, what guards them, addresses and amounts.

WHY THE TEST VECTORS MATTER MORE HERE THAN ANYWHERE ELSE IN THIS REPO. Every other check can be
satisfied by code that is self-consistent; a wallet cannot. A derivation that is wrong in a way that
is wrong the same way every time produces perfectly stable, perfectly useless addresses — money sent
to them is gone, and nothing in the app can tell. So the BTC/ETH/DOGE addresses below are the
PUBLISHED BIP-44 values for the canonical all-`abandon` mnemonic, not values this code produced.

Litecoin is here on a different footing and it is worth saying why. The constant I first wrote down
from memory disagreed with the library, and the library was right: deriving `m/44'/2'/0'/0/0`
through `Bip44` and through raw `Bip32Slip10Secp256k1` yields byte-identical public keys. So LTC is
asserted as a CROSS-DERIVATION (two independent APIs agreeing on the standard path) and is honestly
labelled as such, rather than pretending a remembered string was a citation.
"""
import os

import pytest

from app.services import exodus_wallet_service as W

VECTOR = ("abandon abandon abandon abandon abandon abandon "
          "abandon abandon abandon abandon abandon about")

# Published BIP-44 addresses for VECTOR, no passphrase.
PUBLISHED = {
    "BTC": "1LqBGSKuX5yYUonjxT5qGfpUsXKYYWeabA",
    "ETH": "0x9858EfFD232B4033E47d90003D41EC34EcaEda94",
    "DOGE": "DBus3bamQjgJULBJtYXpEzDWQRwF5iwxgC",
}


def test_derivation_matches_published_bip44_vectors():
    """The whole point. Stable-but-wrong addresses lose money silently."""
    got = W.addresses(VECTOR)
    for symbol, want in PUBLISHED.items():
        assert got.get(symbol) == want, f"{symbol}: derived {got.get(symbol)}, published {want}"


def test_litecoin_agrees_with_a_raw_derivation_of_the_same_path():
    """Cross-derived rather than quoted -- see the module docstring."""
    from bip_utils import Bip32Slip10Secp256k1, Bip39SeedGenerator
    seed = Bip39SeedGenerator(VECTOR).Generate()
    raw = Bip32Slip10Secp256k1.FromSeedAndPath(seed, "m/44'/2'/0'/0/0")
    acct = W._account(seed, "LTC", 0)
    assert acct.PublicKey().RawCompressed().ToHex() == raw.PublicKey().RawCompressed().ToHex()


def test_every_evm_chain_shares_one_address():
    """ETH, Polygon, BNB and Avalanche C-Chain are all coin type 60, which is what Exodus and
    MetaMask do. Deriving a different address per EVM chain would hand somebody four addresses for
    one key and let them publish the wrong one."""
    got = W.addresses(VECTOR)
    evm = {got[s] for s in ("ETH", "MATIC", "BNB", "AVAX") if s in got}
    assert len(evm) == 1, f"EVM chains disagreed about the address: {evm}"


def test_addresses_are_deterministic():
    assert W.addresses(VECTOR) == W.addresses(VECTOR)


def test_a_different_index_is_a_different_address():
    assert W.address_for(VECTOR, "BTC", 0) != W.address_for(VECTOR, "BTC", 1)


# ── the seed and what guards it ───────────────────────────────────────────────────────────────
def test_a_sealed_seed_round_trips():
    key = os.urandom(32)
    blob = W.seal(VECTOR, key)
    assert VECTOR.encode() not in blob, "the mnemonic is in the ciphertext in clear"
    assert W.unseal(blob, key) == VECTOR


def test_the_wrong_key_cannot_open_it():
    blob = W.seal(VECTOR, os.urandom(32))
    with pytest.raises(W.WalletLocked):
        W.unseal(blob, os.urandom(32))


def test_a_tampered_ciphertext_is_refused_rather_than_returning_rubbish():
    """AES-GCM is authenticated. Without the tag check a flipped bit would come back as a DIFFERENT
    valid-looking phrase, which derives a different wallet."""
    key = os.urandom(32)
    blob = bytearray(W.seal(VECTOR, key))
    blob[-1] ^= 0x01
    with pytest.raises(W.WalletLocked):
        W.unseal(bytes(blob), key)


def test_two_seals_of_one_phrase_differ():
    """A fresh nonce every time. Identical ciphertexts would say two accounts share a seed."""
    key = os.urandom(32)
    assert W.seal(VECTOR, key) != W.seal(VECTOR, key)


def test_the_seed_key_is_not_the_storage_key_itself():
    """The storage key already encrypts chats and uploads. One key doing several jobs means a flaw
    in any one of them is a flaw in all, so this derives its own."""
    key = bytes(range(32))
    blob = W.seal(VECTOR, key)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    with pytest.raises(Exception):
        AESGCM(key).decrypt(blob[:12], blob[12:], None)


def test_an_empty_phrase_is_never_stored():
    with pytest.raises(W.WalletError):
        W.seal("   ", os.urandom(32))


def test_an_account_with_no_storage_key_is_locked_not_empty():
    """'Locked' and 'has no wallet' must stay separable: a caller that confuses them offers to
    generate a second seed over the top of the first."""
    with pytest.raises(W.WalletLocked):
        W.seal(VECTOR, b"")


# ── mnemonics ─────────────────────────────────────────────────────────────────────────────────
def test_a_generated_phrase_is_twelve_valid_words():
    m = W.new_mnemonic()
    assert len(m.split()) == 12
    assert W.validate_mnemonic(m)


def test_generated_phrases_differ():
    assert W.new_mnemonic() != W.new_mnemonic()


def test_a_phrase_with_one_wrong_word_is_refused():
    """It would derive a perfectly valid and completely different wallet -- an empty balance for an
    account that is not theirs, with nothing on screen to say why."""
    assert W.validate_mnemonic(VECTOR)
    assert not W.validate_mnemonic(VECTOR.rsplit(" ", 1)[0] + " zoo")


def test_junk_is_refused():
    for bad in ("", "   ", "not a mnemonic at all", "abandon " * 11):
        assert not W.validate_mnemonic(bad)


# ── amounts ───────────────────────────────────────────────────────────────────────────────────
def test_amounts_convert_exactly_in_both_directions():
    for symbol, text, units in (("BTC", "0.1", 10_000_000), ("BTC", "1", 100_000_000),
                                ("ETH", "1", 10 ** 18), ("ETH", "0.000000000000000001", 1),
                                ("SOL", "1.5", 1_500_000_000)):
        assert W.to_base_units(text, symbol) == units
        assert W.from_base_units(units, symbol) == text


def test_binary_floating_point_never_touches_an_amount():
    """0.1 + 0.2 is not 0.3 in float. A wallet that rounds a send sends the wrong number."""
    assert W.to_base_units("0.1", "BTC") + W.to_base_units("0.2", "BTC") == W.to_base_units("0.3", "BTC")


def test_more_precision_than_the_chain_has_is_refused_not_truncated():
    """Somebody who typed nine decimals of BTC meant something; dropping the ninth is a different
    amount than they asked for."""
    with pytest.raises(W.WalletError):
        W.to_base_units("0.000000001", "BTC")


def test_nonsense_amounts_are_refused():
    # "1e9" is here on purpose: Decimal reads it happily, so a slipped keystroke would become a
    # billion units — a number nobody typed and the chain will honour.
    for bad in ("abc", "-1", "0", "", "1e9", "1,000", "0x10", " 1 2 ", "+1", "1."):
        with pytest.raises(W.WalletError):
            W.to_base_units(bad, "BTC")


def test_an_unknown_chain_is_refused_everywhere():
    for call in (lambda: W.to_base_units("1", "NOPE"),
                 lambda: W.from_base_units(1, "NOPE"),
                 lambda: W.address_for(VECTOR, "NOPE")):
        with pytest.raises(W.WalletError):
            call()


def test_monero_is_separate_and_portfolio_specific():
    assert W.MONERO['kind'] == 'monero'
    phrase = 'abandon ' * 11 + 'about'
    assert W.monero_keys(phrase, 0).PrimaryAddress() != W.monero_keys(phrase, 1).PrimaryAddress()


def test_the_catalogue_carries_no_secrets():
    rows = W.supported()
    # The derived chains plus Monero, which is read from the node's own wallet.
    assert {c["symbol"] for c in rows} == set(W.CHAINS) | {"XMR"}
    # The internal bip_utils enum name and anything seed-shaped stay on this side of the wire.
    for row in rows:
        assert set(row) == {"symbol", "name", "decimals", "kind"}, row
    blob = repr(rows)
    for leak in ("BITCOIN_CASH", "BINANCE_SMART_CHAIN", "AVAX_C_CHAIN", "seed", "mnemonic"):
        assert leak not in blob, f"{leak} leaked into the catalogue"


# ── XRP and Monero ────────────────────────────────────────────────────────────────────────────
def test_xrp_derivation_agrees_with_a_raw_derivation_of_the_same_path():
    """Cross-derived, not quoted -- the same discipline LTC needed after a remembered constant
    turned out to be wrong and the library right."""
    from bip_utils import Bip32Slip10Secp256k1, Bip39SeedGenerator, XrpAddrEncoder
    seed = Bip39SeedGenerator(VECTOR).Generate()
    raw = Bip32Slip10Secp256k1.FromSeedAndPath(seed, "m/44'/144'/0'/0/0")
    assert W.address_for(VECTOR, "XRP") == XrpAddrEncoder.EncodeKey(raw.PublicKey().KeyObject())
    assert W._account(seed, "XRP", 0).PublicKey().RawCompressed().ToHex() == \
        raw.PublicKey().RawCompressed().ToHex()


def test_xrp_has_six_decimals_not_eight():
    """XRP's smallest unit is a drop, 1e-6. Two places out is a hundredfold error in a send."""
    assert W.to_base_units("1", "XRP") == 1_000_000
    assert W.to_base_units("0.000001", "XRP") == 1
    assert W.from_base_units(1_500_000, "XRP") == "1.5"
    with pytest.raises(W.WalletError):
        W.to_base_units("0.0000001", "XRP")     # seven decimals


def test_monero_is_offered_but_never_derived_from_this_seed():
    """A second XMR key would give one person two unrelated balances and two addresses, and the one
    they saw last is the one they would publish. The node's existing wallet owns these coins."""
    assert "XMR" not in W.CHAINS, "Monero must not be a derived chain"
    assert "XMR" not in W.addresses(VECTOR)
    with pytest.raises(W.WalletError):
        W.address_for(VECTOR, "XMR")


def test_the_catalogue_lists_the_independent_monero_wallet():
    rows = {r["symbol"]: r for r in W.supported()}
    assert "XMR" in rows and rows["XMR"]["kind"] == "monero"
    assert rows["XRP"]["kind"] == "xrp" and rows["XRP"]["decimals"] == 6
    # Every derived chain still declares a kind a reader can route on.
    assert all(r["kind"] for r in rows.values())


@pytest.mark.parametrize('symbol', ['ETH', 'BTC', 'XMR', 'SOL'])
def test_integer_amounts_preserve_precision_beyond_decimal_context(symbol):
    units = 123456789012345678901234567890123456789
    assert W.to_base_units(W.from_base_units(units, symbol), symbol) == units
