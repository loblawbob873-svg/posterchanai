"""Encrypted Blossom persistence for AI uploads (Phase 2, docs/NOSTR_DATASTORE.md).

Every file uploaded to the AI is stored in the built-in **Blossom** server as **ciphertext**
(NIP-44 over base64 of the bytes, encrypted to the user's server-held storage key), with an
encrypted metadata ref (`pcai:upload:<conv>:<sha>`) recorded alongside the chat. The AI/commands
still receive the **plaintext bytes in memory** (via `build_media_attachments`), so every
upload-consuming feature keeps working — this only adds encrypted at-rest storage on your own infra
(via the storage proxy that Blossom already uses).
"""

import base64
import logging

from . import nostr_store as store
from .nostr_store import user_storage_seckey
from .nostr import nip44, bip340
from . import blossom_service

logger = logging.getLogger(__name__)


async def store_encrypted(db, user, conv_id: int, filename: str, data: bytes, mime: str) -> dict | None:
    """Encrypt `data` to the user's storage key, store the ciphertext in Blossom, record an encrypted
    metadata ref tied to the conversation. Returns the ref ({sha256,name,mime}) or None on failure."""
    try:
        sk = user_storage_seckey(db, user)
        ct = nip44.encrypt_self(sk, base64.b64encode(data).decode()).encode()   # ASCII ciphertext
        pub = bip340.pubkey_from_seckey(sk).hex()
        desc = await blossom_service.save_blob(db, pub, ct, "application/octet-stream")
        sha = desc.get("sha256")
        if not sha:
            return None
        ref = {"sha256": sha, "name": filename, "mime": mime, "size": len(data), "enc": "nip44"}
        await store.put_doc(store_port(db), sk, f"{store.NS_UPLOAD}{conv_id}:{sha}", ref)
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
    return base64.b64decode(nip44.decrypt_self(sk, ct.decode()))


def store_port(db) -> int:
    from app.models import Setting
    row = db.query(Setting).filter(Setting.key == "nostr_relay_port").first()
    return int(row.value) if row and row.value else 3052


async def delete_uploads(db, user, conv_id: int) -> int:
    """Delete a conversation's upload refs (NIP-09). Blob bytes age out via Blossom TTL."""
    sk = user_storage_seckey(db, user)
    port = store_port(db)
    docs = await store.list_docs(port, f"{store.NS_UPLOAD}{conv_id}:", seckey=sk, encrypt=False)
    n = 0
    for d in docs.keys():
        try:
            if await store.delete_doc(port, sk, d):
                n += 1
        except Exception:
            pass
    return n
