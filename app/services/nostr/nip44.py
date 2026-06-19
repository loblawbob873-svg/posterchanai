"""NIP-44 v2 encryption (ChaCha20 + HMAC-SHA256, HKDF key schedule) for server-side Python.

The relay-as-datastore migration stores app data as NIP-44-encrypted Nostr events, but the
server-side Nostr lib only had signing (BIP340) — no encryption. This adds it. ECDH uses
`coincurve` (libsecp256k1) when present, else the repo's pure-Python `bip340` curve math
(a few ms per op — irrelevant at app/bot volume).

Reference: https://github.com/nostr-protocol/nips/blob/master/44.md (version 2).
"""

import os
import hmac
import hashlib
import base64

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

from . import bip340


# ---- ECDH: raw shared X coordinate (NIP-44 uses it UNHASHED, unlike libsecp256k1's default) ----
def _ecdh_x(seckey: bytes, pubkey_xonly: bytes) -> bytes:
    """secp256k1 ECDH → 32-byte X of (seckey * lift_even_y(pubkey))."""
    d = int.from_bytes(seckey, "big")
    P = bip340._lift_x(int.from_bytes(pubkey_xonly, "big"))   # BIP340: x-only → even-y point
    if P is None:
        raise ValueError("invalid public key")
    S = bip340._point_mul(P, d)
    if S is None:
        raise ValueError("invalid ECDH result")
    return bip340._x(S).to_bytes(32, "big")


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out, t, i = b"", b"", 0
    while len(out) < length:
        i += 1
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
    return out[:length]


def get_conversation_key(seckey: bytes, pubkey_xonly: bytes) -> bytes:
    """Symmetric per-pair key: HKDF-extract(salt='nip44-v2', ikm=ecdh_x). Same for (a,B) and (b,A)."""
    shared_x = _ecdh_x(seckey, pubkey_xonly)
    return _hkdf_extract(b"nip44-v2", shared_x)


def _message_keys(conversation_key: bytes, nonce: bytes):
    keys = _hkdf_expand(conversation_key, nonce, 76)
    return keys[0:32], keys[32:44], keys[44:76]   # chacha_key, chacha_nonce(12), hmac_key


def _chacha20(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    # cryptography's ChaCha20 wants a 16-byte nonce = 32-bit LE counter (0) || 96-bit nonce (RFC7539)
    full_nonce = (0).to_bytes(4, "little") + nonce12
    enc = Cipher(algorithms.ChaCha20(key, full_nonce), mode=None).encryptor()
    return enc.update(data) + enc.finalize()


def _calc_padded_len(unpadded: int) -> int:
    if unpadded <= 32:
        return 32
    next_power = 1 << ((unpadded - 1).bit_length())
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * ((unpadded - 1) // chunk + 1)


def _pad(plaintext: bytes) -> bytes:
    n = len(plaintext)
    if n < 1 or n > 65535:
        raise ValueError("plaintext length out of range (1..65535)")
    padded_len = _calc_padded_len(n)
    return n.to_bytes(2, "big") + plaintext + b"\x00" * (padded_len - n)


def _unpad(padded: bytes) -> bytes:
    n = int.from_bytes(padded[:2], "big")
    pt = padded[2:2 + n]
    if len(pt) != n or n < 1:
        raise ValueError("invalid padding")
    return pt


def encrypt(plaintext: str, conversation_key: bytes, nonce: bytes | None = None) -> str:
    """Encrypt a UTF-8 string → base64 NIP-44 v2 payload."""
    nonce = nonce or os.urandom(32)
    if len(nonce) != 32:
        raise ValueError("nonce must be 32 bytes")
    chacha_key, chacha_nonce, hmac_key = _message_keys(conversation_key, nonce)
    ciphertext = _chacha20(chacha_key, chacha_nonce, _pad(plaintext.encode("utf-8")))
    mac = hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()   # AAD = nonce
    return base64.b64encode(b"\x02" + nonce + ciphertext + mac).decode("ascii")


def decrypt(payload_b64: str, conversation_key: bytes) -> str:
    raw = base64.b64decode(payload_b64)
    if not raw or raw[0] != 0x02:
        raise ValueError("unsupported NIP-44 version")
    nonce, ciphertext, mac = raw[1:33], raw[33:-32], raw[-32:]
    chacha_key, chacha_nonce, hmac_key = _message_keys(conversation_key, nonce)
    expected = hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise ValueError("invalid MAC")
    return _unpad(_chacha20(chacha_key, chacha_nonce, ciphertext)).decode("utf-8")


# ---- convenience: encrypt to / decrypt from a specific peer ----
def encrypt_to(seckey: bytes, peer_pubkey_xonly: bytes, plaintext: str) -> str:
    return encrypt(plaintext, get_conversation_key(seckey, peer_pubkey_xonly))


def decrypt_from(seckey: bytes, peer_pubkey_xonly: bytes, payload_b64: str) -> str:
    return decrypt(payload_b64, get_conversation_key(seckey, peer_pubkey_xonly))


# ---- self-encryption (storage to one's own key — the migration's main use) ----
def encrypt_self(seckey: bytes, plaintext: str) -> str:
    return encrypt_to(seckey, bip340.pubkey_from_seckey(seckey), plaintext)


def decrypt_self(seckey: bytes, payload_b64: str) -> str:
    return decrypt_from(seckey, bip340.pubkey_from_seckey(seckey), payload_b64)
