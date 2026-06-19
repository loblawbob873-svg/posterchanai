"""Settings read-path → Nostr relay (Phase 1 of the Nostr-as-datastore migration).

The app reads global settings from the SQLite `Setting` table in ~50 places, via many small
`db.query(Setting)` helpers. Refactoring every caller to read the relay synchronously would be
huge and slow (each read = an async WS round-trip + decrypt). Instead we make the relay the
**authoritative** store and keep the `Setting` table as a fast local **read-through cache**:

  * `hydrate(db)`  — at startup (relay → Setting): pull every `pcai:setting:` doc and UPSERT it
    into the `Setting` table, so all existing synchronous readers transparently see relay values.
  * `write_through(db, changes)` — on admin save (Setting → relay): mirror changed keys to the
    relay so it stays authoritative.

Flag-gated by the `settings_backend` setting (`relay` enables it; default `sqlite` = off, so
production/fresh nodes are unaffected until explicitly flipped). The relay's event store is a
*separate* SQLite file; "no app DB" means the app DB stops being the source of truth, with the
`Setting` table demoted to a derived cache.
"""

import logging

from app.models import Setting
from app.services import nostr_store as store
from app.services import nostr_migrate as _mig

logger = logging.getLogger(__name__)

# Datastore-plumbing keys are always sourced from local SQLite, never hydrated/written-through —
# so a stale relay copy can't change where/whether we connect or flip the backend off mid-flight.
_PLUMBING_KEYS = frozenset({
    "settings_backend", "users_backend", "bots_backend", "records_backend", "chat_backend",
    "nostr_relay_port", "nostr_relay_enabled", "nostr_relay_db_path", "nostr_relay_bind",
})

# Per-node RUNTIME-STATE settings (sync cursors / seen-sets) are advanced directly in SQLite by the
# pollers, NOT via admin Save — so write-through never sees them and the relay copy goes stale. They
# are also inherently per-node (each node has its own sync position), so they must stay local: never
# hydrate (a stale relay cursor would reset progress → re-post old content) and never write-through.
_RUNTIME_KEYS = frozenset({"nitter_seen", "autopost_last_runs", "fedi_timeline_since"})
_RUNTIME_SUFFIXES = ("_since", "_seen", "_cursor", "_last_runs", "_next_batch")


def _is_local_only(key: str) -> bool:
    return key in _PLUMBING_KEYS or key in _RUNTIME_KEYS or key.endswith(_RUNTIME_SUFFIXES)


def enabled(db) -> bool:
    """True when settings should be sourced from the relay (setting settings_backend == 'relay')."""
    row = db.query(Setting).filter(Setting.key == "settings_backend").first()
    return bool(row and (row.value or "").strip().lower() == "relay")


def _operator_seckey(db):
    """The operator's secret key — needed to read/write the operator-signed docs. Sourced from the
    local keyfile (authoritative); falls back to the admin's `User.nostr_nsec` and migrates it into
    the keyfile on first use (so it survives the app DB being eliminated). None if not configured."""
    from app.services import keystore
    from app.services.nostr import nostr_service
    nsec = keystore.get_operator_nsec()
    if not nsec:
        from app.models import User
        op = db.query(User).filter(User.is_admin == True, User.nostr_nsec.isnot(None)).first()  # noqa: E712
        if op and op.nostr_nsec:
            nsec = op.nostr_nsec
            keystore.set_operator_nsec(nsec)   # migrate into the keyfile
    if not nsec:
        return None
    try:
        return nostr_service.decode_seckey(nsec)
    except Exception:
        return None


def _port(db) -> int:
    row = db.query(Setting).filter(Setting.key == "nostr_relay_port").first()
    try:
        return int(row.value) if row and row.value else 3052
    except (TypeError, ValueError):
        return 3052


def _upsert(db, key: str, value: str) -> bool:
    """Write a key into the local Setting cache. Returns True if it changed."""
    row = db.query(Setting).filter(Setting.key == key).first()
    sval = "" if value is None else str(value)
    if row is None:
        db.add(Setting(key=key, value=sval))
        return True
    if (row.value or "") != sval:
        row.value = sval
        return True
    return False


async def hydrate(db) -> int:
    """relay → Setting cache. Pull every setting doc from the relay and UPSERT it into the local
    Setting table so the existing synchronous readers see authoritative (relay) values. No-op when
    disabled or when there's no operator key. Returns the number of keys updated."""
    if not enabled(db):
        return 0
    op_sk = _operator_seckey(db)
    if not op_sk:
        logger.info("[settings-store] hydrate skipped — no operator key")
        return 0
    try:
        relay = await _mig.settings_all(_port(db), op_sk)
    except Exception as e:
        logger.warning("[settings-store] hydrate failed to read relay: %s", e)
        return 0
    if not relay:
        return 0
    changed = 0
    for key, value in relay.items():
        # Plumbing + per-node runtime-state keys are always local (see _is_local_only): a stale relay
        # copy must not change where we connect, flip the backend off, or reset a sync cursor.
        if _is_local_only(key):
            continue
        if _upsert(db, key, value):
            changed += 1
    if changed:
        db.commit()
    logger.info("[settings-store] hydrated %d setting(s) from relay", changed)
    return changed


async def write_through(db, changes: dict) -> int:
    """Setting → relay. Mirror changed settings to the relay so it stays authoritative. `changes`
    is {key: value}. No-op when disabled / no operator key. Returns the number written."""
    if not changes or not enabled(db):
        return 0
    op_sk = _operator_seckey(db)
    if not op_sk:
        return 0
    port = _port(db)
    wrote = 0
    for key, value in changes.items():
        if _is_local_only(key):
            continue
        try:
            ok = await store.put_doc(port, op_sk, store.NS_SETTING + key, {"value": value})
            if ok:
                wrote += 1
        except Exception as e:
            logger.warning("[settings-store] write-through failed for %s: %s", key, e)
    if wrote:
        logger.info("[settings-store] wrote %d setting(s) through to relay", wrote)
    return wrote
