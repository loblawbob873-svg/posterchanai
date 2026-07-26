"""Encrypted Blossom storage for AI artifacts — generated images, output files, etc. (Phase 2).

A produced artifact is encrypted (NIP-44 over base64 of the bytes, to the user's storage key) and
stored in the built-in Blossom server, then referenced by an `image_path` of the form
`<username>/chat/<conv>/enc_<sha256>.<ext>`. `serve_file` recognizes the `enc_<sha>` name, fetches
the ciphertext from Blossom and decrypts it — so existing `/api/files/...` URLs keep working and the
plaintext never hits disk. Blossom still uses the storage proxy it's configured with.
"""

import logging

from .nostr_store import user_storage_seckey
from .nostr import bip340
from . import blossom_service, blobcrypt

logger = logging.getLogger(__name__)


async def save_bytes(db, user, conv_id: int, data: bytes, ext: str, expires_days: int = 0) -> str | None:
    """Encrypt + store an artifact in Blossom; return an image_path the chat references (+ serves).
    `expires_days` > 0 gives the blob an explicit TTL — used for TRANSIENT agent artifacts (workspace
    backups) so they auto-expire instead of accumulating; chat images pass 0 (persist with the chat)."""
    try:
        sk = user_storage_seckey(db, user)
        ct = blobcrypt.encrypt(sk, data)          # AES-256-GCM (handles large images/files)
        pub = bip340.pubkey_from_seckey(sk).hex()
        # private=True: an AI-generated artifact must never appear in the public BUD-02 listing —
        # that listing's sha256 is all /client/file needs to return the decrypted bytes.
        desc = await blossom_service.save_blob(db, pub, ct, "application/octet-stream", private=True,
                                               expires_days=expires_days)
        sha = desc.get("sha256")
        if not sha:
            return None
        ext = (ext or "bin").lstrip(".")
        return f"{user.username}/chat/{conv_id}/enc_{sha}.{ext}"
    except Exception as e:
        logger.warning("[artifact-store] encrypt+store failed: %s", e)
        return None


async def delete_blob(db, sha256: str) -> bool:
    """Remove an artifact's Blossom blob (bytes + row) — used when a chat/its files are deleted."""
    from app.models import BlossomBlob
    blob = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
    if not blob:
        return False
    try:
        await blossom_service.delete_blob_bytes(db, blob)
    except Exception as e:
        logger.debug("[artifact-store] blob bytes delete failed for %s: %s", sha256[:12], e)
    db.delete(blob)
    db.commit()
    return True


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
    return blobcrypt.decrypt(sk, ct)
