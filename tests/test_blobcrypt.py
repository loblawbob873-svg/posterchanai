"""EVERY ENCRYPTED BLOB THIS NODE HOLDS GOES THROUGH TWENTY-FOUR UNTESTED LINES.

`blobcrypt.py` had ZERO test references. It is the AES-256-GCM layer under `upload_store` and
`artifact_store` — images, files, media, AI artifacts — and it exists because NIP-44 caps plaintext
at 65535 bytes and so cannot encrypt a picture.

Small does not mean safe here; it means every property is load-bearing and there is nowhere for a
mistake to hide:

  * **The key derivation is a fork.** `_key` uses a 32-byte secret AS the AES key and sha256s
    anything else. Both branches are live, and changing either one does not fail — it produces a
    DIFFERENT key, so every blob already written becomes permanently unreadable while new writes
    look perfect. Nothing would report it until somebody opened an old file.
  * **The layout is positional.** `nonce(12) || ciphertext+tag`, split by hardcoded `[:12]`/`[12:]`
    on the way back. A changed nonce width breaks decryption of everything previously stored.
  * **Authentication must RAISE.** GCM's tag is the only thing standing between a tampered blob and
    the caller. If a corrupt blob ever came back as bytes instead of an exception, the caller would
    hand attacker-influenced content to whatever asked for the file.

Nothing here asserts a ciphertext VALUE — those are randomised by design — so every test is a
property, and `test_the_nonce_is_not_reused` is the one that would catch the single worst mistake
available in GCM.
"""
import hashlib
import os

import pytest
from cryptography.exceptions import InvalidTag

from app.services import blobcrypt


KEY = b"\x11" * 32


# --------------------------------------------------------------------------- round trip


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 4096, 65535, 65536, 200_000])
def test_a_blob_of_any_size_round_trips(size):
    """65535 is the NIP-44 ceiling this module exists to get past, so the sizes either side of it
    are the point of the whole file — not an edge case."""
    data = os.urandom(size)
    assert blobcrypt.decrypt(KEY, blobcrypt.encrypt(KEY, data)) == data


def test_the_ciphertext_is_not_the_plaintext():
    """A no-op 'encrypt' that returned its input would pass a round-trip test perfectly."""
    data = b"the quick brown fox" * 100
    blob = blobcrypt.encrypt(KEY, data)
    assert data not in blob


def test_the_output_layout_is_nonce_then_ciphertext_and_tag():
    """`decrypt` splits at a hardcoded byte 12. If `encrypt` ever emitted a different nonce width,
    every previously stored blob would stop decrypting — and new ones would still work, so the
    damage would only surface on old files."""
    for size in (0, 1, 5000):
        blob = blobcrypt.encrypt(KEY, os.urandom(size))
        assert len(blob) == 12 + size + 16, "nonce(12) + ciphertext + GCM tag(16)"


# --------------------------------------------------------------------------- the nonce


def test_the_nonce_is_not_reused():
    """THE WORST MISTAKE AVAILABLE IN GCM. Reusing a nonce under one key is catastrophic — it leaks
    the XOR of the plaintexts and destroys the authentication guarantee entirely. A hardcoded or
    derived nonce would pass every other test in this file."""
    nonces = {blobcrypt.encrypt(KEY, b"same plaintext")[:12] for _ in range(200)}
    assert len(nonces) == 200, "nonces repeated across encryptions under one key"


def test_encrypting_the_same_bytes_twice_gives_different_blobs():
    """Deterministic ciphertext leaks equality: an observer of the store could tell which users
    hold the same file without decrypting anything."""
    a = blobcrypt.encrypt(KEY, b"identical")
    b = blobcrypt.encrypt(KEY, b"identical")
    assert a != b
    assert blobcrypt.decrypt(KEY, a) == blobcrypt.decrypt(KEY, b) == b"identical"


# --------------------------------------------------------------------------- key derivation


def test_a_32_byte_secret_is_used_verbatim():
    """The live path — the storage key is already 32 bytes. sha256-ing it 'for consistency' would
    silently orphan every blob on the node."""
    assert blobcrypt._key(KEY) == KEY


@pytest.mark.parametrize("n", [0, 1, 16, 31, 33, 64])
def test_any_other_length_is_sha256ed(n):
    raw = bytes(range(1, n + 1)) if n else b""
    assert blobcrypt._key(raw) == hashlib.sha256(raw).digest()


def test_key_derivation_is_stable_across_calls():
    """It is re-derived on every encrypt and decrypt, so instability means a blob that cannot be
    read back by the process that just wrote it."""
    for raw in (KEY, b"short", b"x" * 64):
        assert blobcrypt._key(raw) == blobcrypt._key(raw)


def test_a_wrong_key_cannot_decrypt():
    blob = blobcrypt.encrypt(KEY, b"secret")
    with pytest.raises(InvalidTag):
        blobcrypt.decrypt(b"\x22" * 32, blob)


def test_a_32_byte_key_and_its_own_hash_are_different_keys():
    """Pins that the two `_key` branches are genuinely distinct, so the parametrised test above is
    not quietly asserting the same thing twice."""
    blob = blobcrypt.encrypt(KEY, b"secret")
    with pytest.raises(InvalidTag):
        blobcrypt.decrypt(hashlib.sha256(KEY).digest(), blob)


# --------------------------------------------------------------------------- authentication


@pytest.mark.parametrize("pos", [0, 5, 11, 12, 13, 40, -1])
def test_flipping_any_byte_is_detected(pos):
    """Covers the nonce (0-11), the ciphertext and the trailing tag. GCM must reject all three;
    returning bytes for any of them would hand corrupted content to the caller as if it were fine."""
    blob = bytearray(blobcrypt.encrypt(KEY, b"authentic content here" * 10))
    blob[pos] ^= 0x01
    with pytest.raises(InvalidTag):
        blobcrypt.decrypt(KEY, bytes(blob))


def test_a_truncated_blob_raises_rather_than_returning_short_data():
    """A partial upload or a torn store read must not silently decrypt to a prefix."""
    blob = blobcrypt.encrypt(KEY, b"content" * 100)
    for cut in (0, 5, 11, 12, 20, len(blob) - 1):
        with pytest.raises(Exception):
            blobcrypt.decrypt(KEY, blob[:cut])


def test_appended_bytes_are_detected():
    blob = blobcrypt.encrypt(KEY, b"content")
    with pytest.raises(InvalidTag):
        blobcrypt.decrypt(KEY, blob + b"extra")


def test_two_blobs_cannot_be_spliced():
    """Swapping one blob's nonce onto another's body must not authenticate — otherwise a store that
    can reorder or mix records could produce a 'valid' file nobody wrote."""
    a = blobcrypt.encrypt(KEY, b"first message")
    b = blobcrypt.encrypt(KEY, b"second message")
    with pytest.raises(InvalidTag):
        blobcrypt.decrypt(KEY, a[:12] + b[12:])
