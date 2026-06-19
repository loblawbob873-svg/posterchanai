"""NIP-04 encrypted DMs (kind 4) for server-side Python — used to notify admins over Nostr.

NIP-04: AES-256-CBC with the raw ECDH shared-X as the key, a random 16-byte IV, content encoded
`<base64 ciphertext>?iv=<base64 iv>`. Legacy but universally readable by Nostr clients (the
built-in client decrypts kind-4 in its worker), which is what we want for an admin notification.
"""

import os
import base64

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as _padding

from .nip44 import _ecdh_x   # raw secp256k1 shared X (NIP-04 uses it directly as the AES-256 key)


def encrypt(seckey: bytes, peer_pubkey_xonly: bytes, text: str) -> str:
    key = _ecdh_x(seckey, peer_pubkey_xonly)
    iv = os.urandom(16)
    pad = _padding.PKCS7(128).padder()
    data = pad.update(text.encode("utf-8")) + pad.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ct = enc.update(data) + enc.finalize()
    return base64.b64encode(ct).decode() + "?iv=" + base64.b64encode(iv).decode()


def decrypt(seckey: bytes, peer_pubkey_xonly: bytes, content: str) -> str:
    ct_b64, iv_b64 = content.split("?iv=")
    key = _ecdh_x(seckey, peer_pubkey_xonly)
    dec = Cipher(algorithms.AES(key), modes.CBC(base64.b64decode(iv_b64))).decryptor()
    data = dec.update(base64.b64decode(ct_b64)) + dec.finalize()
    unpad = _padding.PKCS7(128).unpadder()
    return (unpad.update(data) + unpad.finalize()).decode("utf-8")
