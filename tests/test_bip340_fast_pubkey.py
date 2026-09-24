"""Deriving a pubkey from a secret key must be FAST and must be RIGHT.

Measured 2026-09-24: the relay's startup (`thread._read_config` → `_collect_operator_pubkeys`) derived
every user's storage pubkey once, ~2,400 of them twice over, at ~35ms each in pure Python -- 82 seconds
before the relay listened, on every deploy, with the fediverse answered 503 and the worker unable to
reach it. The native path (libsecp256k1, else OpenSSL) is only enabled after it equals the pure
implementation; a wrong pubkey would make the relay refuse our own writes.
"""
import os
import time

import pytest

from app.services.nostr import bip340


def _native_available() -> bool:
    try:
        import coincurve  # noqa: F401
        return True
    except Exception:
        pass
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        ec.derive_private_key(7, ec.SECP256K1())
        return True
    except Exception:
        return False


def _pure(sk: bytes) -> bytes:
    return bip340._bytes_from_int(bip340._x(bip340._point_mul(bip340.G, int.from_bytes(sk, "big"))))


def test_the_native_derivation_equals_the_pure_one():
    if not _native_available():
        pytest.skip("no native secp256k1 on this machine -- the pure path is the only one")
    for _ in range(64):
        sk = os.urandom(32)
        if not 1 <= int.from_bytes(sk, "big") < bip340.n:
            continue
        bip340._PUBKEY_CACHE.clear()
        assert bip340.pubkey_from_seckey(sk) == _pure(sk)


def test_a_relay_startups_worth_of_keys_derives_in_seconds_not_minutes():
    if not _native_available():
        pytest.skip("no native secp256k1 on this machine")
    keys = [os.urandom(32) for _ in range(2400)]
    bip340._PUBKEY_CACHE.clear()
    t = time.monotonic()
    for sk in keys:
        bip340.pubkey_from_seckey(sk)
    assert time.monotonic() - t < 5, "2,400 derivations took the relay's startup back to minutes"


def test_a_native_path_that_disagrees_is_never_enabled(monkeypatch):
    """Both candidates broken (a wrong answer, a missing curve): the pure path stays."""
    import coincurve
    monkeypatch.setattr(coincurve, "PrivateKey", lambda b: (_ for _ in ()).throw(RuntimeError("no")))
    from cryptography.hazmat.primitives.asymmetric import ec

    class _Wrong:
        def __init__(self, *a):
            pass

        def public_key(self):
            return self

        def public_numbers(self):
            return self
        x = 5
    monkeypatch.setattr(ec, "derive_private_key", lambda d, curve: _Wrong())
    monkeypatch.setattr(bip340, "_fast_pubkey", None)
    bip340._activate_fast_pubkey()
    assert bip340._fast_pubkey is None
    bip340._PUBKEY_CACHE.clear()
    sk = (123456789).to_bytes(32, "big")
    assert bip340.pubkey_from_seckey(sk) == _pure(sk)
