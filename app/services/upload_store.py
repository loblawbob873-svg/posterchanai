"""Encrypted Blossom persistence for AI uploads (Phase 2, docs/NOSTR_DATASTORE.md).

Every file uploaded to the AI is stored in the built-in **Blossom** server as **ciphertext**
(NIP-44 over base64 of the bytes, encrypted to the user's server-held storage key), with an
encrypted metadata ref (`pcai:upload:<conv>:<opaque-id>` — NEVER the sha, see store_encrypted) recorded alongside the chat. The AI/commands
still receive the **plaintext bytes in memory** (via `build_media_attachments`), so every
upload-consuming feature keeps working — this only adds encrypted at-rest storage on your own infra
(via the storage proxy that Blossom already uses).
"""

import logging
import secrets

from . import nostr_store as store
from .nostr_store import user_storage_seckey
from .nostr import bip340
from . import blossom_service, blobcrypt
from app.services import settings_store

logger = logging.getLogger(__name__)


async def store_encrypted(db, user, conv_id: int, filename: str, data: bytes, mime: str) -> dict | None:
    """Encrypt `data` to the user's storage key, store the ciphertext in Blossom, record an encrypted
    metadata ref tied to the conversation. Returns the ref ({sha256,name,mime}) or None on failure."""
    try:
        sk = user_storage_seckey(db, user)
        ct = blobcrypt.encrypt(sk, data)          # AES-256-GCM (handles large files)
        pub = bip340.pubkey_from_seckey(sk).hex()
        desc = await blossom_service.save_blob(db, pub, ct, "application/octet-stream", private=True)
        sha = desc.get("sha256")
        if not sha:
            return None
        ref = {"sha256": sha, "name": filename, "mime": mime, "size": len(data), "enc": "nip44"}
        # The doc id must NOT contain the sha256. Nostr encrypts event CONTENT but never TAGS, so a
        # `d` tag of pcai:upload:<conv>:<sha> published the one secret that /client/file requires to
        # return the DECRYPTED file — on a public relay. An opaque random id leaks nothing; every
        # reader (client.py's listing, delete_uploads, ai-file-delete) takes the sha from the
        # encrypted `ref` body, never from the tag, so this is a drop-in change.
        doc_id = secrets.token_hex(16)
        await store.put_doc(store_port(db), sk, f"{store.NS_UPLOAD}{conv_id}:{doc_id}", ref)
        return ref
    except Exception as e:
        logger.warning("[upload-store] encrypt+store failed for %s: %s", filename, e)
        return None


async def read_decrypted(db, user, sha256: str) -> bytes | None:
    """Fetch + decrypt a stored upload back to its original bytes (server-side, for AI use)."""
    from app.models import BlossomBlob
    sk = user_storage_seckey(db, user)
    blob = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha256).first()
    if not blob:
        return None
    ct = await blossom_service.read_full(db, blob)
    if not ct:
        return None
    return blobcrypt.decrypt(sk, ct)


def store_port(db=None) -> int:
    return settings_store.get_int("nostr_relay_port", 3052)


async def delete_uploads(db, user, conv_id: int) -> int:
    """Delete a conversation's uploads: the encrypted Blossom blobs AND their refs (NIP-09)."""
    from . import artifact_store
    sk = user_storage_seckey(db, user)
    port = store_port(db)
    docs = await store.list_docs(port, f"{store.NS_UPLOAD}{conv_id}:", seckey=sk)   # decrypted refs
    for ref in docs.values():
        if isinstance(ref, dict) and ref.get("sha256"):
            try:
                await artifact_store.delete_blob(db, ref["sha256"])
            except Exception:
                pass
    n = 0
    for d in docs.keys():
        try:
            if await store.delete_doc(port, sk, d):
                n += 1
        except Exception:
            pass
    return n
