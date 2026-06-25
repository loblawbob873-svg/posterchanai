"""Federated, browser-readable mailbox — the Nostr-native replacement for mail_store.

Each message is ONE kind-30078 event **authored by the server's bridge key** (the operator key) and
**NIP-44-encrypted to the recipient's own npub** (`encrypt_to`, not self-encryption), `p`-tagged to
them. d-tag: `pcai:mail:<userpub>:<acct>:<folder>:<uid>`.

Why this shape:
  - **Federated + portable.** `_broadcastable` lets the `pcai:mail:` namespace ride the relay's paced
    outbox to upstream, so the mailbox follows the user across relays/clients — the whole point.
  - **Browser-readable.** Encrypted to the USER's key, so the web client decrypts it with its own
    signer (`nip44dec(bridgePub, content)`) — no server round-trip to read mail.
  - **Server-readable too.** NIP-44's conversation key is ECDH (symmetric), so the bridge can decrypt
    what it wrote — needed for the SMTP side (reply/forward/attachments) which a browser can't do.

Account credentials stay server-side (UserSetting; mail_service.get_user_mail_accounts). Attachments
are AES-GCM encrypted to Blossom; the doc holds only {name,type,size,sha256,key,iv}. mail_sync drives
IMAP→here (producer-paced so a big sync can't overflow the outbox); the client reads from the relay.
"""
import re
import logging

from app.services import nostr_store, settings_store
from app.services.nostr import nostr_service

logger = logging.getLogger(__name__)

NS_MAIL = "pcai:mail:"


def _port() -> int:
    return settings_store.get_int("nostr_relay_port", 3052)


def bridge_seckey(db) -> bytes | None:
    """The bridge identity that authors+encrypts every user's mail — the datastore operator key."""
    return settings_store._operator_seckey(db)


def bridge_pubkey(db) -> str | None:
    sk = bridge_seckey(db)
    return nostr_service.derive_pubkey(sk) if sk else None


def user_pubkey(user) -> str | None:
    """Recipient key (hex) the mail is encrypted+addressed to — the user's login npub."""
    return nostr_service.to_pubkey_hex(getattr(user, "nostr_npub", "") or "")


def _tok(s: str) -> str:
    """d-tag-safe token: ':' is our path separator, squeeze anything non-portable to '_'."""
    return re.sub(r"[^A-Za-z0-9._@-]", "_", (s or "").strip()) or "_"


def _d(user_pk: str, account_email: str, folder: str, uid: str) -> str:
    return f"{NS_MAIL}{_tok(user_pk)}:{_tok(account_email)}:{_tok(folder)}:{_tok(str(uid))}"


def _prefix(user_pk: str, account_email: str | None = None, folder: str | None = None) -> str:
    p = f"{NS_MAIL}{_tok(user_pk)}:"
    if account_email:
        p += _tok(account_email) + ":"
        if folder:
            p += _tok(folder) + ":"
    return p


async def store_message(db, user_pk: str, account_email: str, folder: str, msg: dict) -> bool:
    """Create/replace one message doc, encrypted to `user_pk`. Idempotent on (account, folder, uid)."""
    if not msg or not msg.get("uid"):
        return False
    sk = bridge_seckey(db)
    if not sk:
        logger.warning("[mail-box] no bridge key — cannot store mail")
        return False
    msg = {**msg, "account": account_email, "folder": folder}
    return await nostr_store.put_doc_to(_port(), sk, user_pk, _d(user_pk, account_email, folder, msg["uid"]), msg)


async def get_message(db, user_pk: str, account_email: str, folder: str, uid: str) -> dict | None:
    sk = bridge_seckey(db)
    if not sk:
        return None
    return await nostr_store.get_doc_from(_port(), nostr_service.derive_pubkey(sk),
                                          _d(user_pk, account_email, folder, uid),
                                          reader_sk=sk, counterparty_pubkey=user_pk)


async def list_messages(db, user_pk: str, account_email: str | None = None, folder: str | None = None) -> list:
    """All stored messages under (account[, folder]) for this user — newest first. Server-side read
    (decrypts) — used only by the SMTP-side paths; the GUI reads from the relay in the browser."""
    sk = bridge_seckey(db)
    if not sk:
        return []
    docs = await nostr_store.list_docs_from(_port(), nostr_service.derive_pubkey(sk),
                                            _prefix(user_pk, account_email, folder),
                                            reader_sk=sk, counterparty_pubkey=user_pk, p_tag=user_pk)
    msgs = [v for v in docs.values() if isinstance(v, dict)]
    msgs.sort(key=lambda m: m.get("ts", 0), reverse=True)
    return msgs


async def set_flags(db, user_pk: str, account_email: str, folder: str, uid: str, **flags) -> bool:
    msg = await get_message(db, user_pk, account_email, folder, uid)
    if not msg:
        return False
    f = dict(msg.get("flags") or {})
    f.update(flags)
    msg["flags"] = f
    return await store_message(db, user_pk, account_email, folder, msg)


async def delete_message(db, user_pk: str, account_email: str, folder: str, uid: str) -> bool:
    """Drop the message doc from the mailbox (NIP-09 kind-5, signed by the bridge that authored it)."""
    sk = bridge_seckey(db)
    if not sk:
        return False
    return await nostr_store.delete_doc(_port(), sk, _d(user_pk, account_email, folder, uid))


async def have_uids(db, user_pk: str, account_email: str, folder: str) -> set:
    """UIDs already mirrored for (account, folder) — read from d-tags WITHOUT decrypting (the UID is
    the last d-tag segment), so sync stays cheap on large folders."""
    sk = bridge_seckey(db)
    if not sk:
        return set()
    prefix = _prefix(user_pk, account_email, folder)
    dtags = await nostr_store.list_dtags_from(_port(), nostr_service.derive_pubkey(sk), prefix, p_tag=user_pk)
    return {d[len(prefix):] for d in dtags if len(d) > len(prefix)}


def search(messages: list, query: str) -> list:
    """Case-insensitive substring search over already-loaded messages (subject/from/to/body/preview)."""
    q = (query or "").strip().lower()
    if not q:
        return messages
    out = []
    for m in messages:
        hay = " ".join(str(m.get(k, "")) for k in
                       ("subject", "from", "from_email", "to", "cc", "body_text", "preview", "folder")).lower()
        if q in hay:
            out.append(m)
    return out
