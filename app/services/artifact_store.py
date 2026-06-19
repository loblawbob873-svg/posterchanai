"""Encrypted Blossom storage for AI artifacts — generated images, output files, etc. (Phase 2).

A produced artifact is encrypted (NIP-44 over base64 of the bytes, to the user's storage key) and
stored in the built-in Blossom server, then referenced by an `image_path` of the form
`<username>/chat/<conv>/enc_<sha256>.<ext>`. `serve_file` recognizes the `enc_<sha>` name, fetches
the ciphertext from Blossom and decrypts it — so existing `/api/files/...` URLs keep working and the
plaintext never hits disk. Blossom still uses the storage proxy it's configured with.
"""

import base64
import logging

from .nostr_store import user_storage_seckey
from .nostr import nip44, bip340
from . import blossom_service

logger = logging.getLogger(__name__)


async def save_bytes(db, user, conv_id: int, data: bytes, ext: str) -> str | None:
    """Encrypt + store an artifact in Blossom; return an image_path the chat references (+ serves)."""
    try:
        sk = user_storage_seckey(db, user)
        ct = nip44.encrypt_self(sk, base64.b64encode(data).decode()).encode()
        pub = bip340.pubkey_from_seckey(sk).hex()
        desc = await blossom_service.save_blob(db, pub, ct, "application/octet-stream")
        sha = desc.get("sha256")
        if not sha:
            return None
        ext = (ext or "bin").lstrip(".")
        return f"{user.username}/chat/{conv_id}/enc_{sha}.{ext}"
    except Exception as e:
        logger.warning("[artifact-store] encrypt+store failed: %s", e)
        return None


async def read_bytes(db, user, sha256: str) -> bytes | None:
    """Fetch + decrypt an artifact's bytes (for serve_file)."""
    from app.models import BlossomBlob
    sk = user_storage_seckey(db, user)
    blob = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
    if not blob:
        return None
    ct = await blossom_service.read_full(db, blob)
    if not ct:
        return None
    return base64.b64decode(nip44.decrypt_self(sk, ct.decode()))
