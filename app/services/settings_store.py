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

# Datastore-plumbing keys are always sourced from the local Setting cache, never hydrated/
# written-through — so a stale relay copy can't change where/whether we connect to the relay.
_PLUMBING_KEYS = frozenset({
    "nostr_relay_port", "nostr_relay_enabled", "nostr_relay_bind", "nostr_relay_pg_dsn",
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
    """The relay is the ONLY datastore — always on (the legacy sqlite/table-authoritative mode is
    gone). The Postgres `settings` table is just a hydrated read-cache."""
    return True


def ensure_operator_key(db) -> bool:
    """Resolve (and on a fresh node, MINT) the datastore operator/signer key into the keyfile.
    Must run BEFORE the relay starts so the relay's operator set includes this pubkey — otherwise
    the relay rejects its own first settings doc as "not in web of trust" (the operator key was
    minted lazily, after the relay had already read an empty operator set). Returns True if a key
    is available. Idempotent."""
    try:
        return _operator_seckey(db) is not None
    except Exception as e:
        logger.warning("[settings-store] ensure_operator_key failed: %s", e)
        return False


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
        # No operator key anywhere → mint one and persist it to the keyfile, so a fresh node is a
        # self-sufficient relay datastore (can sign its own settings/users/bots docs). This is what
        # makes "relay backend by default" work out of the box on a brand-new install.
        import secrets
        from app.services.nostr import bech32, bip340
        for _ in range(8):
            cand = secrets.token_bytes(32)
            try:
                bip340.pubkey_from_seckey(cand)
            except Exception:
                continue
            nsec = bech32.encode("nsec", cand)
            keystore.set_operator_nsec(nsec)
            logger.info("[settings-store] minted a fresh operator key (nostr datastore signer)")
            break
    if not nsec:
        return None
    try:
        return nostr_service.decode_seckey(nsec)
    except Exception:
        return None


def ensure_admin(db) -> str | None:
    """Turnkey: on a fresh node with NO admin yet, make the auto-minted OPERATOR key the admin so AI
    works immediately — no manual web 'claim admin' click (which otherwise blocks testing the AI
    parts on a fresh install). Creates an admin User with the operator's npub + full grants and seeds
    the WoT with it. Idempotent + no-op once any admin with an npub exists; gated by
    POSTERCHANAI_AUTO_ADMIN (default on — set 0 to require a human to claim admin with their own key).
    Returns the admin npub if it provisioned/already-operator-admin, else None."""
    import os
    if os.environ.get("POSTERCHANAI_AUTO_ADMIN", "1").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    try:
        from app.models import User, Setting
        from app.services.nostr import nostr_service
        import secrets, binascii
        # An admin with an npub already exists → instance is set up; don't override it.
        if db.query(User).filter(User.is_admin == True, User.nostr_npub.isnot(None)).first():  # noqa: E712
            return None
        op_sk = _operator_seckey(db)
        if not op_sk:
            return None
        pk = nostr_service.derive_pubkey(op_sk)
        pk_hex = pk if isinstance(pk, str) else binascii.hexlify(pk).decode()
        npub = nostr_service.npub_of(pk_hex)
        u = db.query(User).filter(User.nostr_npub == npub).first()
        if not u:
            from app.auth import get_password_hash
            base = "npub_" + npub[4:16]
            username = base
            for i in range(2, 100):
                if not db.query(User).filter(User.username == username).first():
                    break
                username = f"{base}{i}"
            u = User(username=username, email=None,
                     password_hash=get_password_hash(secrets.token_urlsafe(32)),
                     email_verified=True, nostr_npub=npub)
            db.add(u)
        u.is_admin = True
        u.can_ai = True
        u.can_image = True
        u.can_blossom = True
        # Seed the WoT with the admin's own npub (same as the web claim flow) so the relay trusts it.
        row = db.query(Setting).filter(Setting.key == "nostr_relay_wot_seeds").first()
        val = (row.value if row else "") or ""
        if npub not in val:
            nv = (val.rstrip() + "\n" + npub).strip() if val.strip() else npub
            if row:
                row.value = nv
            else:
                db.add(Setting(key="nostr_relay_wot_seeds", value=nv))
        db.commit()
        logger.info("[settings-store] auto-provisioned admin from the operator key: %s", npub[:16])
        return npub
    except Exception as e:
        logger.warning("[settings-store] ensure_admin failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
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


async def seed_relay_defaults(db) -> int:
    """First-boot seeding (relay ← local defaults). Push every local `Setting` whose key the relay
    does NOT yet hold UP to the relay as a `pcai:setting:` doc, so the relay — the authoritative
    datastore — carries the default settings from the very first start instead of them living only
    in the local read-cache. Run AFTER hydrate(): keys the relay already has were just pulled down,
    so they're skipped here (the relay value stays authoritative — never overwritten by a default).
    On an established node nothing is missing, so this writes nothing. No-op when no operator key."""
    if not enabled(db):
        return 0
    op_sk = _operator_seckey(db)
    if not op_sk:
        return 0
    try:
        relay = await _mig.settings_all(_port(db), op_sk)
    except Exception as e:
        logger.warning("[settings-store] seed: failed to read relay: %s", e)
        return 0
    have = set(relay.keys()) if relay else set()
    missing = {}
    for row in db.query(Setting).all():
        if row.key in have or _is_local_only(row.key):
            continue
        missing[row.key] = row.value or ""
    if not missing:
        return 0
    wrote = await write_through(db, missing)
    if wrote:
        logger.info("[settings-store] seeded %d default setting(s) to the relay (first boot)", wrote)
    return wrote
