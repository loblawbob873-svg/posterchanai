"""Pure-Python secp256k1 + BIP340 Schnorr signatures for Nostr.

Nostr signs events with BIP340 Schnorr over secp256k1. The repo's `cryptography`
dep does not expose this, and we deliberately avoid a compiled native dep
(coincurve/secp256k1) for portability across the Arc/ROCm/nas nodes — see the plan.
This is a direct port of the BIP340 reference implementation; signing is a single
scalar multiplication (~ms in pure Python), which is irrelevant at bot/post volume.

Public surface:
    pubkey_from_seckey(seckey: bytes) -> bytes   # 32-byte x-only pubkey
    sign(msg32: bytes, seckey: bytes, aux: bytes | None) -> bytes  # 64-byte sig
    verify(msg32: bytes, pubkey32: bytes, sig64: bytes) -> bool
"""

import hashlib
import os

# secp256k1 domain parameters
p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + msg).digest()


def _is_infinite(point):
    return point is None


def _x(point):
    return point[0]


def _y(point):
    return point[1]


def _point_add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if _x(p1) == _x(p2) and _y(p1) != _y(p2):
        return None
    if p1 == p2:
        lam = (3 * _x(p1) * _x(p1) * pow(2 * _y(p1), p - 2, p)) % p
    else:
        lam = ((_y(p2) - _y(p1)) * pow(_x(p2) - _x(p1), p - 2, p)) % p
    x3 = (lam * lam - _x(p1) - _x(p2)) % p
    return (x3, (lam * (_x(p1) - x3) - _y(p1)) % p)


def _point_mul(point, scalar):
    r = None
    for i in range(256):
        if (scalar >> i) & 1:
            r = _point_add(r, point)
        point = _point_add(point, point)
    return r


def _bytes_from_int(x: int) -> bytes:
    return x.to_bytes(32, byteorder="big")


def _lift_x(x: int):
    if x >= p:
        return None
    y_sq = (pow(x, 3, p) + 7) % p
    y = pow(y_sq, (p + 1) // 4, p)
    if pow(y, 2, p) != y_sq:
        return None
    return (x, y if y & 1 == 0 else p - y)


def _has_even_y(point) -> bool:
    return _y(point) % 2 == 0


_PUBKEY_CACHE: dict = {}
_PUBKEY_CACHE_MAX = 4096


def pubkey_from_seckey(seckey: bytes) -> bytes:
    """Return the 32-byte x-only public key for a 32-byte secret key.

    CACHED (seckey→pubkey): the derivation is a pure-Python secp256k1 point-mul (~35ms), and the same
    key derives its pubkey constantly — the settings hydrate alone called this once per setting doc
    (240× ≈ 8.5s of startup, via nip44.decrypt_self), plus every self-encrypt/decrypt and event sign.
    Deterministic, so the cache is exact; bounded to cap memory. The cache is keyed on a SHA-256 of the
    seckey (not the raw bytes) so no private key material is retained in the long-lived dict — a heap
    scrape / core dump of the cache yields only hashes + public keys."""
    ck = hashlib.sha256(bytes(seckey)).digest()
    v = _PUBKEY_CACHE.get(ck)
    if v is not None:
        return v
    d0 = int.from_bytes(seckey, "big")
    if not (1 <= d0 <= n - 1):
        raise ValueError("secret key out of range")
    P = _point_mul(G, d0)
    v = _bytes_from_int(_x(P))
    if len(_PUBKEY_CACHE) >= _PUBKEY_CACHE_MAX:   # simple bound — cheap to recompute on miss
        _PUBKEY_CACHE.clear()
    _PUBKEY_CACHE[ck] = v
    return v


_fast_sign = None


def sign(msg32: bytes, seckey: bytes, aux: bytes | None = None) -> bytes:
    """BIP340 Schnorr-sign a 32-byte message with a 32-byte secret key.

    Uses libsecp256k1 (coincurve) when it is present and proves itself, falling back to the
    pure-Python scalar maths below. MEASURED on this node: 62 ms per signature pure, 0.099 ms
    through coincurve — 626x. That is not a micro-optimisation here, because every stored document
    is a signed event: mirroring a mail folder of 5000 messages is five minutes of pegged CPU at
    62 ms each, and this app serves on a single uvicorn worker.

    The fast path is only taken when it produces the SAME BYTES as this function for known keys and
    messages (checked once, in _activate_fast_sign). BIP-340 signing is deterministic given the aux,
    so byte-equality is the whole test — a signature that merely verified would mean two
    implementations disagreeing, which is exactly what must not ship silently.
    """
    if aux is None:
        aux = os.urandom(32)
    if _fast_sign is not None:
        try:
            return _fast_sign(msg32, seckey, aux)
        except Exception:
            pass          # fall back to the pure implementation rather than fail a write
    d0 = int.from_bytes(seckey, "big")
    if not (1 <= d0 <= n - 1):
        raise ValueError("secret key out of range")
    P = _point_mul(G, d0)
    d = d0 if _has_even_y(P) else n - d0
    t = (d ^ int.from_bytes(_tagged_hash("BIP0340/aux", aux), "big")).to_bytes(32, "big")
    rand = _tagged_hash("BIP0340/nonce", t + _bytes_from_int(_x(P)) + msg32)
    k0 = int.from_bytes(rand, "big") % n
    if k0 == 0:
        raise RuntimeError("nonce generation failed")
    R = _point_mul(G, k0)
    k = k0 if _has_even_y(R) else n - k0
    e = int.from_bytes(
        _tagged_hash("BIP0340/challenge", _bytes_from_int(_x(R)) + _bytes_from_int(_x(P)) + msg32),
        "big",
    ) % n
    sig = _bytes_from_int(_x(R)) + _bytes_from_int((k + e * d) % n)
    if not verify(msg32, _bytes_from_int(_x(P)), sig):
        raise RuntimeError("signing produced an invalid signature")
    return sig


# Optional fast path: libsecp256k1 via coincurve is ~2400x faster than the pure-Python verify
# (~0.03ms vs ~67ms), which matters a LOT when the relay mass-verifies synced events. Defined
# here so `verify` below can use it; activated by a self-test AFTER `verify` is defined (the
# reference `sign` verifies its own output, so the self-test needs `verify` to exist first).
_fast_verify = None
try:
    from coincurve import PublicKeyXOnly as _CCXOnly

    def _cc_verify(msg32: bytes, pubkey32: bytes, sig64: bytes) -> bool:
        try:
            return _CCXOnly(pubkey32).verify(sig64, msg32)
        except Exception:
            return False
except Exception:
    _cc_verify = None


def verify(msg32: bytes, pubkey32: bytes, sig64: bytes) -> bool:
    """Verify a 64-byte BIP340 Schnorr signature."""
    if len(pubkey32) != 32 or len(sig64) != 64 or len(msg32) != 32:
        return False
    if _fast_verify is not None:
        return _fast_verify(msg32, pubkey32, sig64)
    P = _lift_x(int.from_bytes(pubkey32, "big"))
    if P is None:
        return False
    r = int.from_bytes(sig64[:32], "big")
    s = int.from_bytes(sig64[32:], "big")
    if r >= p or s >= n:
        return False
    e = int.from_bytes(
        _tagged_hash("BIP0340/challenge", sig64[:32] + pubkey32 + msg32), "big"
    ) % n
    R = _point_add(_point_mul(G, s), _point_mul(P, n - e))
    if R is None or not _has_even_y(R) or _x(R) != r:
        return False
    return True


def _activate_fast_verify() -> None:
    """Enable the coincurve fast path only if it computes a known good/bad signature correctly.
    Runs now that `verify` exists (the self-test's `sign` verifies its own output)."""
    global _fast_verify
    if _cc_verify is None:
        return
    try:
        tsk = (7).to_bytes(32, "big")
        tpub = pubkey_from_seckey(tsk)
        tmsg = bytes(range(32))
        tsig = sign(tmsg, tsk)   # _fast_verify still None here → sign's self-check uses pure-Python
        if _cc_verify(tmsg, tpub, tsig) and not _cc_verify(tmsg, tpub, b"\x00" * 64):
            _fast_verify = _cc_verify
    except Exception:
        _fast_verify = None


_activate_fast_verify()


def _activate_fast_sign() -> None:
    """Enable libsecp256k1 signing only if it is byte-for-byte identical to the pure implementation.

    Stricter than the verify guard on purpose: a wrong VERIFY is caught by the check itself, but a
    wrong SIGN would be written into events and published before anyone noticed.
    """
    global _fast_sign
    try:
        from coincurve import PrivateKey as _CCPriv
    except Exception:
        return

    def _cc_sign(msg32: bytes, seckey: bytes, aux: bytes) -> bytes:
        return _CCPriv(seckey).sign_schnorr(msg32, aux)

    try:
        for i in (1, 7, 255):
            tsk = i.to_bytes(32, "big")
            tmsg = hashlib.sha256(bytes([i])).digest()
            for taux in (b"\x00" * 32, bytes(range(32))):
                if _cc_sign(tmsg, tsk, taux) != sign(tmsg, tsk, taux):   # _fast_sign still None
                    return
        _fast_sign = _cc_sign
    except Exception:
        _fast_sign = None


_activate_fast_sign()
