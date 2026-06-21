"""Global settings store — Nostr relay is the ONLY datastore (no SQL `Setting` table).

Settings live as operator-signed `pcai:setting:` events in the relay's Postgres event store. The
app keeps an **in-process cache** (`_CACHE`) hydrated from the relay at startup, so the many
synchronous readers (`settings_store.get(...)`) are fast — no SQL `Setting` table, no per-read WS
round-trip. Writes go to the cache and are mirrored to the relay (`put`/`put_many`, via a
background relay-writer). Per-node LOCAL-ONLY keys (plumbing needed to reach the relay + runtime
cursors) can't live in the relay, so they persist in a small `local_settings.json` in the data dir.

  * `load_local()`            — load local-only keys from the JSON file (call before the relay starts)
  * `apply_defaults(d)`       — seed default values into the cache for keys not already present
  * `hydrate(db)`             — relay → cache (authoritative for shareable keys)
  * `seed_relay_defaults(db, d)` — first boot: push defaults the relay lacks UP to the relay
  * `get/get_bool/get_int/get_float/all_settings/prefixed/exists` — sync reads
  * `put/put_many/delete`    — writes (local JSON for local-only, relay for shareable)
"""

import logging
import os
import json
import threading

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


# ============================================================================================
# In-process settings store (NO SQL Setting table). The relay is authoritative for shareable
# settings (hydrated into _CACHE at startup); local-only keys (plumbing + per-node runtime cursors)
# persist in a small JSON file in the data dir. All ~app-wide settings reads/writes go through the
# get*/put/delete accessors below — there is no `Setting` ORM table anymore.
# ============================================================================================
_CACHE: dict = {}
_lock = threading.RLock()
_loaded = False
_HYDRATED_KEYS: set = set()   # keys for which the relay holds an authoritative value (vs a default)

_LOCAL_PATH = os.environ.get(
    "POSTERCHANAI_LOCAL_SETTINGS",
    os.path.join(os.environ.get("POSTERCHANAI_DATA", "/var/lib/posterchanai"), "local_settings.json"),
)


def _load_local_file() -> dict:
    try:
        with open(_LOCAL_PATH) as f:
            d = json.load(f)
            return {str(k): ("" if v is None else str(v)) for k, v in d.items()} if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("[settings-store] could not read %s: %s", _LOCAL_PATH, e)
        return {}


def _save_local_file() -> None:
    """Persist ONLY the local-only keys (plumbing + runtime cursors) to the JSON file. Atomic."""
    local = {k: v for k, v in _CACHE.items() if _is_local_only(k)}
    tmp = _LOCAL_PATH + ".tmp"
    try:
        os.makedirs(os.path.dirname(_LOCAL_PATH), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(local, f)
        os.replace(tmp, _LOCAL_PATH)
    except Exception as e:
        logger.warning("[settings-store] could not write %s: %s", _LOCAL_PATH, e)


# ---- synchronous read accessors (replace every db.query(Setting) read) ----
def get(key: str, default=None):
    """The value for `key` (str) from the in-process cache, or `default` if unset."""
    with _lock:
        v = _CACHE.get(key)
    return default if v is None else v


def get_bool(key: str, default: bool = False) -> bool:
    v = get(key, None)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def get_int(key: str, default: int = 0) -> int:
    try:
        v = get(key, None)
        return default if v is None or v == "" else int(v)
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    try:
        v = get(key, None)
        return default if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return default


def all_settings() -> dict:
    with _lock:
        return dict(_CACHE)


def prefixed(prefix: str) -> dict:
    with _lock:
        return {k: v for k, v in _CACHE.items() if k.startswith(prefix)}


def exists(key: str) -> bool:
    with _lock:
        return key in _CACHE


# ---- writes ----
def _set_local(key: str, value) -> bool:
    """Update the in-process cache (+ persist local-only keys to the JSON file). Returns changed."""
    sval = "" if value is None else str(value)
    with _lock:
        if _CACHE.get(key) == sval:
            return False
        _CACHE[key] = sval
        local = _is_local_only(key)
    if local:
        _save_local_file()
    return True


def put(key: str, value, *, write_relay: bool = True) -> None:
    """Set a setting. Local-only keys persist to the JSON file; shareable keys also write through to
    the relay (best-effort, fire-and-forget from sync callers)."""
    changed = _set_local(key, value)
    if not changed or _is_local_only(key) or not write_relay:
        return
    _schedule_relay_write({key: "" if value is None else str(value)})


def put_many(changes: dict, *, write_relay: bool = True) -> None:
    relay = {}
    for k, v in (changes or {}).items():
        if _set_local(k, v) and not _is_local_only(k):
            relay[k] = "" if v is None else str(v)
    if write_relay and relay:
        _schedule_relay_write(relay)


def delete(key: str) -> None:
    with _lock:
        existed = key in _CACHE
        _CACHE.pop(key, None)
        local = _is_local_only(key)
    if local:
        _save_local_file()
    elif existed:
        _schedule_relay_delete(key)


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
        from app.models import User
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
        db.commit()
        # Seed the WoT with the admin's own npub via the relay-authoritative settings store.
        val = get("nostr_relay_wot_seeds", "") or ""
        if npub not in val:
            put("nostr_relay_wot_seeds", (val.rstrip() + "\n" + npub).strip() if val.strip() else npub)
        logger.info("[settings-store] auto-provisioned admin from the operator key: %s", npub[:16])
        return npub
    except Exception as e:
        logger.warning("[settings-store] ensure_admin failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _port() -> int:
    return get_int("nostr_relay_port", 3052)


# ---- startup population of the in-process cache ----
def load_local() -> None:
    """Load the local-only keys (plumbing + cursors) from the JSON file into the cache. Call EARLY
    (before the relay starts) so nostr_relay_port/bind/enabled are available to reach the relay."""
    with _lock:
        _CACHE.update(_load_local_file())


def apply_defaults(defaults: dict) -> None:
    """Seed default values into the cache for keys not already present (so reads work out of the box
    even before the relay hydrate). Relay values override these in hydrate(). Local-only defaults are
    also persisted to the JSON file."""
    wrote_local = False
    with _lock:
        for k, v in (defaults or {}).items():
            if k not in _CACHE:
                _CACHE[k] = "" if v is None else str(v)
                if _is_local_only(k):
                    wrote_local = True
    if wrote_local:
        _save_local_file()


def hydrate_from_db(db) -> int:
    """relay events → cache, SYNCHRONOUSLY, by reading the relay's `events` table directly (it's in
    the same Postgres) + NIP-44-decrypting each `pcai:setting:` doc. No WebSocket — so this works
    before the relay's WS is up AND inside the relay subprocess itself (which reads its own config).
    Authoritative for shareable keys; local-only keys are left to the JSON file. Caches the operator
    key for the background relay-writer. Returns the number of keys updated."""
    global _OP_SK
    op_sk = _operator_seckey(db)
    if not op_sk:
        return 0
    _OP_SK = op_sk
    try:
        from app.services.nostr import nostr_service, nip44
        import binascii, json as _json
        from sqlalchemy import text as _text
        pk = nostr_service.derive_pubkey(op_sk)
        op_hex = pk if isinstance(pk, str) else binascii.hexlify(pk).decode()
        rows = db.execute(_text(
            "SELECT DISTINCT ON (t.value) t.value AS d, e.content "
            "FROM events e JOIN event_tags t ON t.event_id = e.id "
            "WHERE e.kind = 30078 AND e.pubkey = :pk AND t.tag = 'd' AND t.value LIKE 'pcai:setting:%' "
            "ORDER BY t.value, e.created_at DESC"
        ), {"pk": op_hex}).fetchall()
    except Exception as e:
        logger.warning("[settings-store] hydrate_from_db failed: %s", e)
        return 0
    changed = 0
    for d, content in rows:
        key = d[len(store.NS_SETTING):]
        if _is_local_only(key):
            continue
        try:
            data = _json.loads(nip44.decrypt_self(op_sk, content))
            val = data.get("value") if isinstance(data, dict) else data
            _HYDRATED_KEYS.add(key)   # the relay holds an authoritative value for this key
            if _set_local(key, "" if val is None else str(val)):
                changed += 1
        except Exception:
            continue
    logger.info("[settings-store] hydrated %d setting(s) from relay events (sync)", changed)
    return changed


def migrate_legacy_table(db) -> int:
    """ONE-TIME data-safety: if a pre-no-db node still has the old SQL `settings` table, copy any key
    the relay does NOT already hold into the relay (via put → relay event), so the node's CUSTOM
    values survive the table going away. Keys the relay already has (hydrated above) and local-only
    keys are skipped. Idempotent; harmless when the table is gone. Returns rows migrated."""
    from sqlalchemy import text as _text, inspect as _inspect
    try:
        if not _inspect(db.bind).has_table("settings"):
            return 0
        rows = db.execute(_text("SELECT key, value FROM settings")).fetchall()
    except Exception as e:
        logger.warning("[settings-store] legacy table read failed: %s", e)
        return 0
    migrated = 0
    for key, value in rows:
        if _is_local_only(key) or key in _HYDRATED_KEYS:
            continue
        put(key, value if value is not None else "")   # legacy value beats the code default
        _HYDRATED_KEYS.add(key)
        migrated += 1
    if migrated:
        logger.info("[settings-store] migrated %d legacy settings-table key(s) into the relay", migrated)
    return migrated


async def hydrate(db) -> int:
    """relay → cache. Pull every setting doc from the relay into the in-process cache (authoritative
    for shareable keys). Caches the operator key for the background relay-writer. No-op without an
    operator key. Returns the number of keys updated."""
    global _OP_SK
    op_sk = _operator_seckey(db)
    if not op_sk:
        logger.info("[settings-store] hydrate skipped — no operator key")
        return 0
    _OP_SK = op_sk
    try:
        relay = await _mig.settings_all(_port(), op_sk)
    except Exception as e:
        logger.warning("[settings-store] hydrate failed to read relay: %s", e)
        return 0
    changed = 0
    for key, value in (relay or {}).items():
        if _is_local_only(key):   # a stale relay copy must not change plumbing or reset a cursor
            continue
        if _set_local(key, value):
            changed += 1
    logger.info("[settings-store] hydrated %d setting(s) from relay", changed)
    return changed


async def write_through(db, changes: dict) -> int:
    """Mirror settings → relay (authoritative). Used by the background writer and admin save."""
    if not changes:
        return 0
    op_sk = _OP_SK or _operator_seckey(db)
    if not op_sk:
        return 0
    port = _port()
    wrote = 0
    for key, value in changes.items():
        if _is_local_only(key):
            continue
        try:
            if await store.put_doc(port, op_sk, store.NS_SETTING + key, {"value": value}):
                wrote += 1
        except Exception as e:
            logger.warning("[settings-store] write-through failed for %s: %s", key, e)
    if wrote:
        logger.info("[settings-store] wrote %d setting(s) through to relay", wrote)
    return wrote


async def seed_relay_defaults(db, defaults: dict) -> int:
    """First-boot seeding (relay ← defaults). Push every default whose key the relay does NOT yet
    hold UP to the relay, so it carries the out-of-box config from first start. Run AFTER hydrate()
    so keys the relay already has are skipped (never overwritten). Writes nothing on an established
    node. `defaults` is the canonical default_settings dict (NOT a table)."""
    op_sk = _OP_SK or _operator_seckey(db)
    if not op_sk:
        return 0
    try:
        relay = await _mig.settings_all(_port(), op_sk)
    except Exception as e:
        logger.warning("[settings-store] seed: failed to read relay: %s", e)
        return 0
    have = set((relay or {}).keys())
    missing = {k: ("" if v is None else str(v)) for k, v in (defaults or {}).items()
               if k not in have and not _is_local_only(k)}
    if not missing:
        return 0
    # also reflect into the cache so reads are correct immediately
    for k, v in missing.items():
        _set_local(k, v)
    wrote = await write_through(db, missing)
    if wrote:
        logger.info("[settings-store] seeded %d default setting(s) to the relay (first boot)", wrote)
    return wrote


# ---- background relay writer: bridges synchronous put()/delete() to async relay writes ----
_OP_SK = None
_writer_q = None
_writer_started = False


def _ensure_writer() -> None:
    global _writer_q, _writer_started
    if _writer_started:
        return
    import queue as _queue
    import asyncio as _asyncio
    _writer_q = _queue.Queue()

    def _run():
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        while True:
            op = _writer_q.get()
            if not op:
                continue
            kind, payload = op
            try:
                if kind == "put":
                    loop.run_until_complete(write_through(None, payload))
                elif kind == "delete" and (_OP_SK):
                    loop.run_until_complete(store.delete_doc(_port(), _OP_SK, store.NS_SETTING + payload))
            except Exception as e:
                logger.warning("[settings-store] relay writer error: %s", e)

    threading.Thread(target=_run, daemon=True, name="settings-relay-writer").start()
    _writer_started = True


def _schedule_relay_write(changes: dict) -> None:
    _ensure_writer()
    _writer_q.put(("put", dict(changes)))


def _schedule_relay_delete(key: str) -> None:
    _ensure_writer()
    _writer_q.put(("delete", key))
