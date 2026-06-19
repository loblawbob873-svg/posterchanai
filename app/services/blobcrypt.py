"""Symmetric blob encryption for large AI artifacts/uploads (images, files, media).

NIP-44 caps plaintext at 65535 bytes (it's for messages), so it can't encrypt images. Blobs use
**AES-256-GCM** with the user's 32-byte server-held storage key as the AES key: output is
`nonce(12) || ciphertext+tag`. Authenticated, arbitrary size, fast. (Chat *text* still uses NIP-44.)
"""

import os
import hashlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _key(seckey: bytes) -> bytes:
    return seckey if len(seckey) == 32 else hashlib.sha256(seckey).digest()


def encrypt(seckey: bytes, data: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(_key(seckey)).encrypt(nonce, data, None)


def decrypt(seckey: bytes, blob: bytes) -> bytes:
    return AESGCM(_key(seckey)).decrypt(blob[:12], blob[12:], None)
