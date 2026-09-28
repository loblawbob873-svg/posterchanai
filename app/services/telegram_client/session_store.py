"""The Telegram session string, at rest: ONE kind-30078 doc per account, `d=pcai:tgsession`,
NIP-44-encrypted to that account's server-held storage key (nostr_store.user_storage_seckey).

The session IS the login — whoever holds it reads and writes that person's Telegram — so it is never
logged, never returned to a browser, and never written anywhere else. Reads are STRICT: a relay that
cannot be asked raises instead of answering "no session", or a restart during a relay hiccup would
look like a signed-out account and invite a second login.
"""
from __future__ import annotations

D = "pcai:tgsession"


def _port():
    from app.services import settings_store
    return settings_store._port()


async def load(db, user) -> str:
    from app.services import nostr_store
    sk = nostr_store.user_storage_seckey(db, user)
    doc = await nostr_store.get_doc(_port(), D, seckey=sk, strict=True)
    if isinstance(doc, dict) and isinstance(doc.get("s"), str) and not doc.get("gone"):
        return doc["s"]
    return ""


async def save(db, user, session: str) -> bool:
    from app.services import nostr_store
    sk = nostr_store.user_storage_seckey(db, user)
    return bool(await nostr_store.put_doc(_port(), sk, D, {"v": 1, "s": str(session or "")}))


async def clear(db, user) -> bool:
    """A tombstone, not a deletion: the replaceable doc is REPLACED by one that says signed out."""
    from app.services import nostr_store
    sk = nostr_store.user_storage_seckey(db, user)
    return bool(await nostr_store.put_doc(_port(), sk, D, {"v": 1, "gone": True}))
