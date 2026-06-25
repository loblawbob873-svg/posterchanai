"""IMAP → Nostr-mailbox sync + encrypted-Blossom attachments.

`sync_all` is the login-triggered ("log in → fetch your mail") and manual-refresh entry point: for
each configured account it pulls recent messages from IMAP (mail_service), AES-GCM-encrypts every
attachment and stores the ciphertext in Blossom, then writes each message as an encrypted kind-30078
doc via mail_store. Only genuinely new UIDs are stored (dedup), and the new-message count per account
is returned so the GUI can badge/notify.

Attachments never touch the relay in the clear: the message doc holds only {name,type,size,sha256,
key,iv}; `load_attachment` fetches the Blossom ciphertext and decrypts it on download (and the compose
path reuses it to re-attach the real bytes to an outgoing SMTP message — exactly like a normal client).
"""
import os
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.services import mail_store, mail_service, blossom_service, nostr_store
from app.services.nostr import nostr_service

logger = logging.getLogger(__name__)

# Folders mirrored by default + how many recent messages per folder. Bounded so a huge mailbox can't
# explode the relay; the GUI can pull more on demand later.
SYNC_FOLDERS = ["INBOX", "Sent"]
SYNC_LIMIT = 50


# --- attachment crypto (AES-256-GCM; ciphertext = iv-free, iv stored alongside in the doc) ---------

def _aes_encrypt(data: bytes) -> tuple[bytes, bytes, bytes]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = os.urandom(32)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, data, None)
    return ct, key, iv


def _aes_decrypt(ct: bytes, key: bytes, iv: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key).decrypt(iv, ct, None)


async def _store_attachment(db: Session, owner_pubkey: str, att) -> dict:
    """Encrypt one EmailAttachment and stash the ciphertext in Blossom. Returns the doc ref."""
    ct, key, iv = _aes_encrypt(att.data or b"")
    desc = await blossom_service.save_blob(db, owner_pubkey, ct, "application/octet-stream")
    return {
        "name": att.filename or "attachment",
        "type": att.content_type or "application/octet-stream",
        "size": int(att.size or len(att.data or b"")),
        "sha256": desc.get("sha256"),
        "key": key.hex(),
        "iv": iv.hex(),
    }


# NIP-44 caps encrypted plaintext at 65535 bytes; HTML emails routinely exceed that. Offload the
# bodies to an encrypted Blossom blob below this size so any message stores as a (small) event.
_DOC_MAX = 50000


async def offload_body(db: Session, owner_pubkey: str, doc: dict) -> dict:
    """If the serialized doc would blow the NIP-44 plaintext cap, move body_text/body_html into an
    encrypted Blossom blob and keep just a `body_ref` + the inline `preview`. Idempotent-ish: a doc
    that already fits is returned unchanged."""
    try:
        if len(json.dumps(doc)) <= _DOC_MAX:
            return doc
    except Exception:
        return doc
    payload = json.dumps({"body_text": doc.get("body_text", ""), "body_html": doc.get("body_html", "")}).encode("utf-8")
    try:
        ct, key, iv = _aes_encrypt(payload)
        desc = await blossom_service.save_blob(db, owner_pubkey, ct, "application/octet-stream")
        doc["body_ref"] = {"sha256": desc.get("sha256"), "key": key.hex(), "iv": iv.hex()}
        doc["body_text"] = ""
        doc["body_html"] = ""
    except Exception as e:
        logger.warning("[mail-sync] body offload failed (%s) — truncating to fit", e)
        doc["body_html"] = ""
        doc["body_text"] = (doc.get("body_text", "") or "")[:40000]
    return doc


async def load_body(db: Session, ref: dict) -> dict | None:
    """Fetch + decrypt an offloaded body blob → {body_text, body_html}, or None if gone."""
    pt, _ = await load_attachment(db, {"sha256": (ref or {}).get("sha256"),
                                       "key": (ref or {}).get("key"), "iv": (ref or {}).get("iv")})
    if pt is None:
        return None
    try:
        return json.loads(pt.decode("utf-8"))
    except Exception:
        return None


async def load_attachment(db: Session, ref: dict) -> tuple[bytes | None, str]:
    """Fetch + decrypt an attachment's bytes from Blossom (for download or to re-attach on forward).
    Returns (plaintext_bytes, mime) or (None, '') if the blob is gone."""
    from app.models import BlossomBlob
    sha = (ref or {}).get("sha256")
    if not sha:
        return None, ""
    blob = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha).first()
    if not blob:
        return None, ""
    ct = await blossom_service.read_full(db, blob)
    if ct is None:
        return None, ""
    try:
        pt = _aes_decrypt(ct, bytes.fromhex(ref["key"]), bytes.fromhex(ref["iv"]))
    except Exception as e:
        logger.warning("[mail-sync] attachment decrypt failed (%s): %s", sha[:12], e)
        return None, ""
    return pt, ref.get("type") or "application/octet-stream"


# --- message → doc -------------------------------------------------------------------------------

def _to_doc(msg, att_refs: list) -> dict:
    """Map an EmailMessage (+ stored attachment refs) to the JSON we persist as the encrypted doc."""
    dt = msg.date if isinstance(msg.date, datetime) else None
    ts = int(dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()) if dt else 0
    return {
        "uid": str(msg.uid),
        "message_id": msg.message_id or "",
        "from": msg.sender or msg.sender_email or "",
        "from_email": msg.sender_email or "",
        "to": msg.to or "",
        "subject": msg.subject or "(no subject)",
        "date": dt.isoformat() if dt else "",
        "ts": ts,
        "body_text": msg.body_text or "",
        "body_html": msg.body_html or "",
        "preview": (msg.body_text or "")[:200],   # kept inline even when the body is offloaded (list view)
        "in_reply_to": msg.in_reply_to or "",
        "references": msg.references or "",
        "flags": {"read": bool(msg.is_read)},
        "attachments": att_refs,
    }


async def _sync_folder(db: Session, user, seckey: bytes, owner_pk: str, account, folder: str) -> int:
    """Mirror recent messages of one (account, folder) into the encrypted mailbox. Returns new count."""
    try:
        import asyncio
        msgs = await asyncio.to_thread(mail_service.fetch_messages, account, folder, SYNC_LIMIT, False)
    except Exception as e:
        logger.warning("[mail-sync] fetch %s/%s failed: %s", account.email, folder, e)
        return 0
    have = await mail_store.have_uids(seckey, account.email, folder)
    new = 0
    for m in msgs or []:
        if str(m.uid) in have:
            continue
        att_refs = []
        for att in (m.attachments or []):
            try:
                att_refs.append(await _store_attachment(db, owner_pk, att))
            except Exception as e:
                logger.warning("[mail-sync] attachment store failed for %s: %s", m.uid, e)
        try:
            doc = await offload_body(db, owner_pk, _to_doc(m, att_refs))
            if await mail_store.store_message(seckey, account.email, folder, doc):
                new += 1
        except Exception as e:
            logger.warning("[mail-sync] store msg %s failed: %s", m.uid, e)
    return new


async def sync_one(db: Session, user, account_email: str, folder: str) -> int:
    """Sync a single (account, folder) on demand — used when the GUI opens a folder that isn't in the
    default INBOX/Sent set. Returns new-message count (0 for the local-only Drafts folder)."""
    if folder == "Drafts":
        return 0
    acc = next((a for a in mail_service.get_user_mail_accounts(user.id, db) if a.email == account_email), None)
    if not acc:
        return 0
    seckey = nostr_store.user_storage_seckey(db, user)
    owner_pk = nostr_service.derive_pubkey(seckey)
    return await _sync_folder(db, user, seckey, owner_pk, acc, folder)


async def sync_all(db: Session, user, folders: list | None = None) -> dict:
    """Sync every configured account → encrypted mailbox. Returns {account_email: new_count}.
    Called on login and on manual refresh; new counts drive the GUI's unread badge / notification."""
    accounts = mail_service.get_user_mail_accounts(user.id, db)
    if not accounts:
        return {}
    seckey = nostr_store.user_storage_seckey(db, user)
    owner_pk = nostr_service.derive_pubkey(seckey)
    folders = folders or SYNC_FOLDERS
    out = {}
    for acc in accounts:
        total = 0
        for folder in folders:
            total += await _sync_folder(db, user, seckey, owner_pk, acc, folder)
        out[acc.email] = total
    if any(out.values()):
        logger.info("[mail-sync] %s: new mail %s", getattr(user, "username", "?"),
                    {k: v for k, v in out.items() if v})
    return out
