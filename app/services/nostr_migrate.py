"""One-time migrations from `app.db` (SQLite) into the relay event store (docs/NOSTR_DATASTORE.md).

Idempotent: kind-30078 docs are parameterized-replaceable, so re-running simply refreshes them —
safe to run repeatedly (e.g. on every boot until the flag flips the read path over).
"""

import logging

from . import nostr_store as store

logger = logging.getLogger(__name__)

# (The old `settings` table → relay migration helpers were removed: the relay event store is now
#  the only datastore, so there's no SQL `Setting` table to migrate from. The read accessors below
#  are still used by settings_store.)


# ---- read accessors (used once the flag points the settings read path at the relay) ----
async def setting_get(port: int, operator_seckey: bytes, key: str, default=None):
    doc = await store.get_doc(port, store.NS_SETTING + key, seckey=operator_seckey)
    if isinstance(doc, dict) and "value" in doc:
        return doc["value"]
    return default


async def settings_all(port: int, operator_seckey: bytes) -> dict:
    docs = await store.list_docs(port, store.NS_SETTING, seckey=operator_seckey)
    out = {}
    for d, v in docs.items():
        out[d[len(store.NS_SETTING):]] = v.get("value") if isinstance(v, dict) else v
    return out


async def purge_app_docs(port: int, operator_seckey: bytes, prefix: str = "pcai:") -> int:
    """Delete the operator-signed app-data docs (kind-30078, `pcai:` d-tags) from the relay — the
    'delete AI notes' action for testing / re-running the migration. Returns how many were removed.
    (User-signed content like chats uses each user's own key and isn't touched here.)"""
    docs = await store.list_docs(port, prefix, seckey=operator_seckey, encrypt=False)  # keys only
    removed = 0
    for d in docs.keys():
        try:
            if await store.delete_doc(port, operator_seckey, d):
                removed += 1
        except Exception as e:
            logger.warning("[migrate] purge of %s failed: %s", d, e)
    logger.info("[migrate] purged %d app-data doc(s) under %s", removed, prefix)
    return removed
