"""One-time migrations from `app.db` (SQLite) into the relay event store (docs/NOSTR_DATASTORE.md).

Idempotent: kind-30078 docs are parameterized-replaceable, so re-running simply refreshes them —
safe to run repeatedly (e.g. on every boot until the flag flips the read path over).
"""

import logging

from app.models import Setting
from . import nostr_store as store

logger = logging.getLogger(__name__)


# ---- settings: the whole `settings` table → encrypted operator-signed docs ----
async def migrate_settings(db, port: int, operator_seckey: bytes) -> dict:
    """Copy EVERY row of the `settings` table into the relay (one encrypted kind-30078 doc per key,
    value preserved verbatim as {"value": ...}). Returns a completeness report so nothing is lost."""
    rows = db.query(Setting).all()
    written = 0
    for s in rows:
        ok = await store.put_doc(port, operator_seckey, store.NS_SETTING + s.key,
                                 {"value": s.value if s.value is not None else ""})
        if ok:
            written += 1
        else:
            logger.warning("[migrate] setting '%s' failed to write to the relay", s.key)
    report = await verify_settings(db, port, operator_seckey)
    logger.info("[migrate] settings → relay: wrote %d/%d, %d missing %s",
                written, len(rows), len(report["missing"]),
                ("(" + ", ".join(report["missing"][:10]) + ")") if report["missing"] else "")
    report["written"] = written
    return report


async def verify_settings(db, port: int, operator_seckey: bytes) -> dict:
    """Compare the `settings` table against what's now in the relay so we can prove no key was lost."""
    db_keys = {s.key for s in db.query(Setting).all()}
    relay = await settings_all(port, operator_seckey)
    missing = sorted(db_keys - set(relay.keys()))
    return {"db": len(db_keys), "relay": len(relay), "missing": missing}


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
