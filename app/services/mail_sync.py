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
import base64
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.services import mail_store, mail_service, blossom_service, nostr_store
from app.services.nostr import nostr_service

logger = logging.getLogger(__name__)


async def _save_blob(owner_pubkey: str, data: bytes, mime: str = "application/octet-stream") -> dict:
    """Blossom save on a FRESH short-lived DB session. A full mailbox sync interleaves many save_blob
    calls with long IMAP/relay awaits; reusing the request session lets Postgres kill its idle
    transaction mid-sync ('rollback() fully before proceeding') and every later write fails."""
    from app.database import SessionLocal
    _db = SessionLocal()
    try:
        # keep=True: an attachment (or an offloaded body) is the ONLY copy of that mail's content —
        # the message doc holds a reference, not the bytes. Blossom's age sweep is driven LIVE by
        # `blossom_blob_ttl_days`, so without this an admin turning that setting on later
        # retroactively deletes every attachment in every mailbox, leaving message docs pointing at
        # a dead sha with nothing to say it happened. This is the same trap Notes hit; `keep` only
        # ever goes False→True, because dedup means one set of bytes can be both a throwaway and
        # something irreplaceable.
        return await blossom_service.save_blob(_db, owner_pubkey, data, mime, keep=True)
    finally:
        try:
            _db.close()
        except Exception:
            pass

# How many messages per folder to mirror is governed by the `mail_sync_limit` setting (0 = all,
# the default — full Nostr mailbox); see _sync_limit(). All of an account's real folders are synced.


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
    desc = await _save_blob(owner_pubkey, ct)
    return {
        "name": att.filename or "attachment",
        "type": att.content_type or "application/octet-stream",
        "size": int(att.size or len(att.data or b"")),
        "sha256": desc.get("sha256"),
        "key": key.hex(),
        "iv": iv.hex(),
    }


# NIP-44 caps encrypted plaintext at 65535 BYTES (not chars). Stay well under it (metadata + base64
# padding overhead + headroom); offload the bodies — and any oversized inline attachment — to encrypted
# Blossom blobs so any message/draft stores as a (small) event.
_DOC_MAX = 48000


def _doc_bytes(doc: dict) -> int:
    try:
        return len(json.dumps(doc).encode("utf-8"))   # NIP-44 limit is on UTF-8 BYTES
    except Exception:
        return _DOC_MAX + 1


async def store_b64_attachment(db: Session, owner_pubkey: str, att: dict) -> dict:
    """Encrypt a compose/draft attachment ({name,type,b64}) → Blossom; return a {sha256,key,iv} ref."""
    raw = base64.b64decode(att.get("b64") or "")
    ct, key, iv = _aes_encrypt(raw)
    desc = await _save_blob(owner_pubkey, ct)
    return {"name": att.get("name") or "attachment", "type": att.get("type") or "application/octet-stream",
            "size": len(raw), "sha256": desc.get("sha256"), "key": key.hex(), "iv": iv.hex()}


async def offload_body(db: Session, owner_pubkey: str, doc: dict) -> dict:
    """Keep the encrypted doc under the NIP-44 byte cap: move body_text/body_html (and, if still too
    big, any inline b64 attachments — e.g. drafts) into encrypted Blossom blobs, leaving small refs +
    the inline `preview`. A doc that already fits is returned unchanged."""
    if _doc_bytes(doc) <= _DOC_MAX:
        return doc
    payload = json.dumps({"body_text": doc.get("body_text", ""), "body_html": doc.get("body_html", "")}).encode("utf-8")
    try:
        ct, key, iv = _aes_encrypt(payload)
        desc = await _save_blob(owner_pubkey, ct)
        doc["body_ref"] = {"sha256": desc.get("sha256"), "key": key.hex(), "iv": iv.hex()}
        doc["body_text"] = ""
        doc["body_html"] = ""
    except Exception as e:
        logger.warning("[mail-sync] body offload failed (%s) — truncating to fit", e)
        doc["body_html"] = ""
        doc["body_text"] = (doc.get("body_text", "") or "")[:30000]
    # Still too big? Oversized inline attachments (drafts) — push them to Blossom too.
    if _doc_bytes(doc) > _DOC_MAX and doc.get("attachments"):
        out = []
        for a in doc["attachments"]:
            if a.get("b64"):
                try:
                    out.append(await store_b64_attachment(db, owner_pubkey, a))
                    continue
                except Exception as e:
                    logger.warning("[mail-sync] draft attachment offload failed: %s", e)
            out.append(a)
        doc["attachments"] = out
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

def _logical_of(folder: str, meta: dict) -> str:
    """Map a real folder name to a logical one (INBOX/Sent/Drafts/Trash/Spam/Archive) so the unified
    'All inboxes' view can filter consistently across accounts whose real folder names differ."""
    if (folder or "").upper() == "INBOX":
        return "INBOX"
    for key, name in (("sent", "Sent"), ("drafts", "Drafts"), ("trash", "Trash"), ("junk", "Spam"), ("archive", "Archive")):
        if meta.get(key) == folder:
            return name
    return folder


def _to_doc(msg, att_refs: list, logical: str = "") -> dict:
    """Map an EmailMessage (+ stored attachment refs) to the JSON we persist as the encrypted doc."""
    dt = msg.date if isinstance(msg.date, datetime) else None
    ts = int(dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()) if dt else 0
    return {
        "uid": str(msg.uid),
        "logical": logical or "",
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


def _sync_limit() -> int:
    """How many messages per folder to mirror. 0 = EVERYTHING (default) — full Nostr mailbox."""
    from app.services import settings_store
    try:
        return max(0, settings_store.get_int("mail_sync_limit", 0))
    except Exception:
        return 0


async def _sync_folder(db: Session, user, seckey: bytes, owner_pk: str, account, folder: str, limit: int = 0, logical: str = "") -> int:
    """INCREMENTAL mirror of one (account, folder): cheap UID SEARCH → diff against what's stored →
    FETCH only the NEW messages → encrypt attachments/large bodies to Blossom → store. limit>0 caps to
    the most-recent N new (0 = all). Returns count stored."""
    import asyncio
    try:
        uids = await asyncio.to_thread(mail_service.list_uids, account, folder)
    except Exception as e:
        logger.warning("[mail-sync] list_uids %s/%s failed: %s", account.email, folder, e)
        return 0
    if not uids:
        return 0
    have = await mail_store.have_uids(seckey, account.email, folder)
    new = [u for u in uids if str(u) not in have]
    if not new:
        return 0
    if limit and limit > 0:
        new = new[-limit:]            # SEARCH is oldest→newest, so keep the most recent N
    try:
        msgs = await asyncio.to_thread(mail_service.fetch_by_uids, account, folder, new)
    except Exception as e:
        logger.warning("[mail-sync] fetch %s/%s failed: %s", account.email, folder, e)
        return 0
    stored = 0
    for m in msgs or []:
        att_refs = []
        for att in (m.attachments or []):
            try:
                att_refs.append(await _store_attachment(db, owner_pk, att))
            except Exception as e:
                logger.warning("[mail-sync] attachment store failed for %s: %s", m.uid, e)
        try:
            doc = await offload_body(db, owner_pk, _to_doc(m, att_refs, logical))
            if await mail_store.store_message(seckey, account.email, folder, doc):
                stored += 1
        except Exception as e:
            logger.warning("[mail-sync] store msg %s failed: %s", m.uid, e)
    return stored


def _account_meta(db, user, account) -> tuple:
    """(folders, special-use meta) for an account, on a FRESH session (runs mid-sync after long IMAP
    awaits). Skips Gmail's All-Mail to avoid mirroring twice. Logical 'Drafts' is local-only."""
    from app.database import SessionLocal
    _db = SessionLocal()
    try:
        meta = mail_service.list_special_folders(user.id, _db, account.email)
        allf = [f for f in (meta.get("all") or []) if "all mail" not in f.lower()]
        return (allf or ["INBOX"], meta)
    except Exception:
        return (["INBOX"], {})
    finally:
        try:
            _db.close()
        except Exception:
            pass


async def sync_one(db: Session, user, account_email: str, folder: str) -> int:
    """Sync a single (account, folder) on demand (e.g. opening a folder). Drafts is local-only."""
    if folder == "Drafts":
        return 0
    acc = next((a for a in mail_service.get_user_mail_accounts(user.id, db) if a.email == account_email), None)
    if not acc:
        return 0
    seckey = nostr_store.user_storage_seckey(db, user)
    owner_pk = nostr_service.derive_pubkey(seckey)
    import asyncio
    _, meta = await asyncio.to_thread(_account_meta, db, user, acc)
    return await _sync_folder(db, user, seckey, owner_pk, acc, folder,
                              await asyncio.to_thread(_sync_limit), _logical_of(folder, meta))


async def sync_all(db: Session, user, folders: list | None = None) -> dict:
    """Sync EVERY account's EVERY real folder into the encrypted mailbox (incremental). Returns
    {account_email: new_count}. limit per folder from `mail_sync_limit` (0 = everything, the default)."""
    import asyncio
    accounts = mail_service.get_user_mail_accounts(user.id, db)
    if not accounts:
        return {}
    seckey = nostr_store.user_storage_seckey(db, user)
    owner_pk = nostr_service.derive_pubkey(seckey)
    limit = await asyncio.to_thread(_sync_limit)
    out = {}
    for acc in accounts:
        if folders is not None:
            flist, meta = folders, {}
        else:
            flist, meta = await asyncio.to_thread(_account_meta, db, user, acc)
        total = 0
        for folder in flist:
            total += await _sync_folder(db, user, seckey, owner_pk, acc, folder, limit, _logical_of(folder, meta))
        out[acc.email] = total
    if any(out.values()):
        logger.info("[mail-sync] %s: new mail %s", getattr(user, "username", "?"),
                    {k: v for k, v in out.items() if v})
    return out
